"""Databento DBN loading for backtesting, plus a historical download helper.

Reads `.dbn` / `.dbn.zst` OHLCV files — or a single merged `.parquet` / `.csv`
produced by `tools/merge_data.py` — and turns them into `Bar` objects stamped
in *server* time, which is what the strategy expects. The merged format carries
the same columns (ts_event, open, high, low, close, volume, symbol), so every
downstream rule (spread filtering, contract selection, rolling) is identical
whichever you load.

Parent-symbology files
----------------------
A file downloaded with `stype_in="parent"` (e.g. `GC.FUT`) contains **every**
contract of the product at once, and also every calendar **spread**
(`GCG5-GCJ5`, which quotes a price difference and is often negative).  Feeding
that straight into a backtest would interleave unrelated instruments on the
same timestamp and produce meaningless ranges.

So this loader:

  1. drops spread instruments (any symbol containing "-"),
  2. de-duplicates on (timestamp, symbol) rather than timestamp alone,
  3. picks ONE contract to trade, per `contract_mode`:

     * ``front_month_volume`` (default) — for each *server* trading date, the
       outright contract with the highest volume, with the roll forced to move
       forward only (it can never fall back to an earlier expiry).  The roll
       happens on a date boundary, so a session's range and its trades always
       come from a single contract.
     * ``symbol`` — one explicit contract, e.g. ``GCG5``.
     * ``all`` — no selection; only correct for single-instrument files.
"""
from __future__ import annotations

import glob
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from ..bars import Bar
from ..timeutils import ServerClock

_PRICE_SCALE = 1e-9          # Databento fixed-point price scale
_OHLC = ("open", "high", "low", "close")

# CME month codes
_MONTH_CODE = {c: i + 1 for i, c in enumerate("FGHJKMNQUVXZ")}
_CONTRACT_RE = re.compile(r"^(?P<root>[A-Z0-9]+?)(?P<m>[FGHJKMNQUVXZ])(?P<y>\d{1,2})$")


# --------------------------------------------------------------------------
def _expand_paths(paths) -> List[str]:
    if isinstance(paths, str):
        paths = [paths]
    out: List[str] = []
    for p in paths:
        if any(ch in p for ch in "*?["):
            out.extend(sorted(glob.glob(p)))
        elif os.path.isdir(p):
            for pat in ("*.dbn*", "*.parquet", "*.csv"):
                out.extend(sorted(glob.glob(os.path.join(p, pat))))
        else:
            out.append(p)
    missing = [p for p in out if not os.path.exists(p)]
    if missing:
        raise FileNotFoundError(f"DBN file(s) not found: {missing}")
    if not out:
        raise FileNotFoundError(f"No DBN files matched: {paths}")
    return out


def contract_rank(symbol: str, ref_year: int) -> Optional[int]:
    """Sortable expiry rank for a futures contract code (GCZ4 -> 2024*12+12).

    Single-digit years are resolved against `ref_year`, so GCF7 seen in 2026
    means January 2027, not 2017.
    """
    m = _CONTRACT_RE.match(symbol.strip().upper())
    if not m:
        return None
    mon = _MONTH_CODE[m.group("m")]
    ydigits = m.group("y")
    if len(ydigits) == 2:
        year = 2000 + int(ydigits)
    else:
        year = (ref_year // 10) * 10 + int(ydigits)
        if year < ref_year - 1:          # digit wrapped into the next decade
            year += 10
    return year * 12 + mon


def is_spread(symbol: str) -> bool:
    return "-" in symbol or ":" in symbol


def _as_clamp(value):
    """Normalise a start/end clamp to (UTC pandas Timestamp, is_date_only)."""
    import pandas as pd
    if isinstance(value, tuple):                 # already parsed by orb.cli
        ts, date_only = value
        return pd.Timestamp(ts).tz_convert("UTC"), bool(date_only)
    if isinstance(value, datetime):
        ts = pd.Timestamp(value)
        ts = ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")
        return ts, (ts.hour == 0 and ts.minute == 0 and ts.second == 0)
    from ..cli import parse_clamp
    dt, date_only = parse_clamp(str(value))
    return pd.Timestamp(dt).tz_convert("UTC"), date_only


# --------------------------------------------------------------------------
def _read_flat(path: str, include_spreads: bool):
    """Read a merged bar file written by tools/merge_data.py (.parquet / .csv)."""
    import pandas as pd
    if path.lower().endswith(".csv"):
        df = pd.read_csv(path, parse_dates=["ts_event"])
    else:
        df = pd.read_parquet(path)
    if "ts_event" not in df.columns:
        df = df.reset_index()
    missing = {"ts_event", *_OHLC} - set(df.columns)
    if missing:
        raise ValueError(f"{os.path.basename(path)} is missing column(s): "
                         f"{sorted(missing)}")
    df = df.set_index("ts_event")
    if "symbol" not in df.columns:
        df["symbol"] = "UNKNOWN"
    if not include_spreads:
        df = df[~df["symbol"].map(is_spread)]
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df.index = pd.to_datetime(df.index, utc=True)
    return df[[*_OHLC, "volume", "symbol"]].copy()


def _read_one(path: str, include_spreads: bool):
    if path.lower().endswith((".parquet", ".pq", ".csv")):
        return _read_flat(path, include_spreads)

    import databento as db
    import pandas as pd

    store = db.DBNStore.from_file(path)
    schema = str(getattr(store, "schema", "") or "")
    if schema and not schema.startswith("ohlcv"):
        raise ValueError(
            f"{os.path.basename(path)} has schema '{schema}'. The backtester "
            f"needs an OHLCV schema (ohlcv-1s / ohlcv-1m / ohlcv-1h / ohlcv-1d). "
            f"Trade/quote schemas such as tbbo or mbp-1 are not bar data.")
    df = store.to_df()
    if df is None or df.empty:
        return None

    if "symbol" not in df.columns:
        df = df.copy()
        df["symbol"] = str(getattr(store, "symbols", ["UNKNOWN"])[0])
    if not include_spreads:
        df = df[~df["symbol"].map(is_spread)]
    if df.empty:
        return None

    keep = [c for c in (*_OHLC, "volume", "symbol") if c in df.columns]
    df = df[keep].copy()
    if "volume" not in df.columns:
        df["volume"] = 0.0

    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError(f"{os.path.basename(path)}: no ts_event DatetimeIndex.")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")

    for col in _OHLC:
        if col not in df.columns:
            raise ValueError(f"{os.path.basename(path)} is missing '{col}'.")
        if str(df[col].dtype).startswith(("int", "uint")):
            df[col] = df[col].astype("float64") * _PRICE_SCALE
    return df


def _select_front_month(df, log=None, min_volume: float = 0.0,
                        boundary_hour: int = 18):
    """Per trading-day highest-volume contract, rolling forward only.

    `df` must already be indexed by *server* time.  Returns (filtered_df, rolls).

    The trading day starts at `boundary_hour` server time, NOT at midnight.
    CME opens the next trading day at 18:00 New York, so a bar stamped 20:15 on
    the 27th belongs to trade date the 28th and already trades the new contract.
    Bucketing on midnight instead puts the instrument change at 00:00, which is
    the middle of any session that spans midnight: the range gets built on the
    old contract and the breakout is judged against the new one, so the entire
    calendar spread (+$33 to +$60 on gold) enters the series as a price move
    that never happened.  Anchoring on 18:00 puts the change on the session
    boundary, where no session is open.
    """
    import pandas as pd

    work = df.copy()
    # shift the clock so that `boundary_hour` lands on midnight, then take the
    # date — this is the CME trade date as the exchange itself assigns it
    shift = pd.Timedelta(hours=(24 - int(boundary_hour)) % 24)
    work["_date"] = (work.index + shift).normalize()
    vol = work.groupby(["_date", "symbol"])["volume"].sum()

    chosen: Dict[pd.Timestamp, str] = {}
    rolls: List[Tuple[object, str, float]] = []
    held: Optional[str] = None
    held_rank = -1

    for date, group in vol.groupby(level=0):
        g = group.droplevel(0).sort_values(ascending=False)
        g = g[g >= min_volume] if min_volume else g
        if g.empty:
            continue
        ref_year = int(pd.Timestamp(date).year)
        leader = str(g.index[0])
        lead_rank = contract_rank(leader, ref_year)
        if lead_rank is None:                  # unparseable code: take it as-is
            chosen[date] = leader
            continue
        if held is None or lead_rank >= held_rank or held not in g.index:
            if leader != held:
                # report the wall-clock instant the instrument actually changes,
                # not the trade date it belongs to — they differ by `shift`
                rolls.append((pd.Timestamp(date) - shift, leader, float(g.iloc[0])))
            held, held_rank = leader, lead_rank
        chosen[date] = held

    if not chosen:
        raise ValueError("Could not determine a front-month contract — no volume "
                         "found in the data.")

    want = work["_date"].map(chosen)
    out = work[work["symbol"].values == want.values].drop(columns=["_date"])

    if log:
        log.info(f"Contract selection: front month by daily volume "
                 f"| trading day starts {int(boundary_hour):02d}:00 server time "
                 f"| {len(rolls)} contract(s) used")
        for d, sym, v in rolls:
            log.info(f"  roll {d:%Y.%m.%d %H:%M} -> {sym}  (volume {v:,.0f})")
    return out, rolls


# --------------------------------------------------------------------------
def load_dbn_bars(paths, clock: ServerClock,
                  contract_mode: str = "front_month_volume",
                  contract_symbol: Optional[str] = None,
                  include_spreads: bool = False,
                  roll_min_volume: float = 0.0,
                  roll_boundary_hour: int = 18,
                  start: Optional[str] = None,
                  end: Optional[str] = None,
                  logger=None) -> List[Bar]:
    """Load one or more DBN OHLCV files into a time-sorted list of `Bar`.

    Parameters
    ----------
    paths            file path, list of paths, directory or glob pattern
    clock            ServerClock used to convert UTC -> broker server time
    contract_mode    front_month_volume | symbol | all
    contract_symbol  the contract to use when contract_mode == "symbol"
    include_spreads  keep calendar spreads (almost never what you want)
    roll_min_volume  ignore contracts below this daily volume when rolling
    roll_boundary_hour  server-time hour the trading day starts on, and the
                     only instant the contract may change (18 = CME open)
    start, end       optional 'YYYY-MM-DD' clamps, applied in UTC
    """
    try:
        import databento as db  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "The `databento` package is required to read DBN files.\n"
            "Install it with:  pip install databento"
        ) from exc
    import pandas as pd

    files = _expand_paths(paths)
    frames = []
    for path in files:
        d = _read_one(path, include_spreads)
        if d is not None:
            frames.append(d)
            if logger:
                logger.debug(f"{os.path.basename(path)}: {len(d):,} outright rows, "
                             f"{d['symbol'].nunique()} contract(s)")
    if not frames:
        raise ValueError("No OHLCV records found in the given DBN file(s).")

    df = pd.concat(frames)
    df = df.sort_index()

    # files often overlap; a timestamp legitimately carries many contracts, so
    # the duplicate key is (timestamp, symbol) — never the timestamp alone
    df = df[~df.index.to_frame().assign(s=df["symbol"].values)
            .duplicated(keep="last").values]

    # start/end accept "YYYY-MM-DD" or a full "YYYY-MM-DD HH:MM" (UTC).
    # A date-only end includes that whole day; a time is taken literally.
    if start is not None:
        ts, _ = _as_clamp(start)
        df = df[df.index >= ts]
    if end is not None:
        ts, date_only = _as_clamp(end)
        if date_only:
            ts = ts + pd.Timedelta(days=1)
        df = df[df.index < ts]
    if df.empty:
        raise ValueError("No bars left after the start/end filter.")

    # ---- convert to server time BEFORE rolling, so the roll lands on a
    #      trading-day boundary as the strategy sees it
    if clock._tz is not None:                                   # noqa: SLF001
        idx = df.index.tz_convert(clock._tz).tz_localize(None)  # noqa: SLF001
    else:
        idx = df.index.tz_convert("UTC").tz_localize(None) + clock._fixed  # noqa: SLF001
    df.index = idx

    # ---- pick the instrument ------------------------------------------
    mode = (contract_mode or "front_month_volume").strip().lower()
    symbols = df["symbol"].unique()
    if mode == "symbol":
        if not contract_symbol:
            raise ValueError("contract_mode='symbol' needs `contract_symbol`.")
        df = df[df["symbol"] == contract_symbol]
        if df.empty:
            raise ValueError(
                f"Contract '{contract_symbol}' not found. Available: "
                f"{', '.join(sorted(map(str, symbols))[:40])}")
        if logger:
            logger.info(f"Contract selection: fixed symbol {contract_symbol}")
    elif mode == "all":
        if len(symbols) > 1 and logger:
            logger.warn(f"contract_mode='all' but the data holds {len(symbols)} "
                        f"instruments — bars from different contracts will be "
                        f"interleaved. This is almost certainly wrong.")
    else:
        df, _ = _select_front_month(df, log=logger, min_volume=roll_min_volume,
                                    boundary_hour=roll_boundary_hour)

    df = df.sort_index()
    if logger:
        logger.info(f"Loaded {len(df):,} bars | {df.index[0]:%Y.%m.%d %H:%M} .. "
                    f"{df.index[-1]:%Y.%m.%d %H:%M} server time "
                    f"| from {len(files)} file(s)")

    arr = df[["open", "high", "low", "close", "volume"]].to_numpy(dtype="float64")
    times = df.index.to_pydatetime()
    return [Bar(t, float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
            for t, r in zip(times, arr)]


def list_contracts(paths, include_spreads: bool = False) -> "object":
    """Inspection helper: what instruments are in these files, and how liquid?"""
    import pandas as pd
    frames = [d for d in (_read_one(p, include_spreads) for p in _expand_paths(paths))
              if d is not None]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames)
    out = df.groupby("symbol").agg(
        bars=("close", "size"), volume=("volume", "sum"),
        first=("close", lambda s: s.index.min()),
        last=("close", lambda s: s.index.max()))
    return out.sort_values("volume", ascending=False)


def infer_base_seconds(bars: Sequence[Bar], default: int = 60) -> int:
    """Detect the source bar resolution from the most common timestamp delta."""
    if len(bars) < 3:
        return default
    from collections import Counter
    deltas: Counter = Counter()
    for a, b in zip(bars[:5000], bars[1:5001]):
        d = int((b.time - a.time).total_seconds())
        if d > 0:
            deltas[d] += 1
    return deltas.most_common(1)[0][0] if deltas else default


# --------------------------------------------------------------------------
def download_history(cfg, out_dir: Optional[str] = None) -> str:
    """Download a Databento historical OHLCV range to a .dbn.zst file."""
    try:
        import databento as db
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("pip install databento") from exc

    if not cfg.api_key:
        raise RuntimeError("No Databento API key. Set `databento.api_key` in the "
                           "config or the DATABENTO_API_KEY environment variable.")
    out_dir = out_dir or cfg.output_dir
    os.makedirs(out_dir, exist_ok=True)
    safe = str(cfg.symbols).replace("/", "_").replace(",", "-")
    fname = f"{cfg.dataset}_{safe}_{cfg.schema}_{cfg.start}_{cfg.end}.dbn.zst"
    path = os.path.join(out_dir, fname)

    client = db.Historical(cfg.api_key)
    data = client.timeseries.get_range(
        dataset=cfg.dataset, symbols=cfg.symbols, schema=cfg.schema,
        stype_in=cfg.stype_in, start=cfg.start, end=cfg.end)
    data.to_file(path)
    return path
