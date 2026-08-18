"""Databento Live feed.

Subscribes to an OHLCV schema and yields completed base bars, converted to
broker/server time.  Records arrive on a background thread and are handed to
the trading loop through a queue, so the loop can keep ticking (for stop-time
handling) even when the market is quiet.

INSTRUMENT FILTERING — read this before changing anything here.

`stype_in="parent"` with `symbols="GC.FUT"` does NOT stream one instrument. It
streams EVERY instrument under that parent:

  * ~35 outright contracts (GCZ5, GCG6, GCJ6 ...) trading simultaneously, and
  * every calendar spread (GCZ5-GCG6 ...), which quotes a price DIFFERENCE and
    is frequently NEGATIVE.

`orb/data/dbn.py` has always solved this for backtests — spreads dropped,
de-duplication keyed on (timestamp, symbol), one contract selected. This module
did not, and the consequence was visible in a live journal: three consecutive
"breakouts" printed closes of 4452.900, **-104.100** and 4417.300, all fed into
one resampler as if they were the same instrument. Only `dry_run: true` stopped
orders going out against a calendar spread.

So this feed now identifies every bar before accepting it:

  1. `SymbolMappingMsg` records map `instrument_id` -> raw symbol. They are
     captured as they arrive.
  2. Spreads are dropped (`is_spread`).
  3. If a contract is locked (the warm-up passes the front month it selected),
     only that contract is kept.
  4. Two independent backstops catch anything that slips through: a
     non-positive price, and a price outside a sanity band around the last
     accepted one.

A bar that cannot be identified is DROPPED, never traded. Silence is a
recoverable problem; an order priced off a calendar spread is not.
"""
from __future__ import annotations

import queue
import threading
from datetime import datetime, timezone
from typing import Iterator, Optional, Tuple

from ..bars import Bar
from ..data.dbn import is_spread
from ..timeutils import ServerClock

_PRICE_SCALE = 1e-9

#: How far from the last accepted close a bar may sit before it is treated as a
#: different instrument. Gold moves a few percent on a violent day; a calendar
#: spread or a mis-scaled record misses by orders of magnitude. Wide enough
#: never to reject a real move, tight enough to catch anything that is not the
#: contract we think we are trading.
SANITY_BAND = 0.25          # +/- 25%


class DatabentoLiveFeed:
    def __init__(self, cfg, clock: ServerClock, logger=None):
        try:
            import databento as db
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("pip install databento") from exc
        if not cfg.api_key:
            raise RuntimeError(
                "No Databento API key. Put DATABENTO_API_KEY in `.env` at the "
                "project root — copy `.env.example` to `.env` and fill it in. "
                "A real environment variable of the same name also works and "
                "wins over the file. The key is deliberately NOT in the engine "
                "config, which is tracked in git.")
        self._db = db
        self.cfg = cfg
        self.clock = clock
        self.log = logger
        self._queue: "queue.Queue[Optional[Bar]]" = queue.Queue()
        self._client = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.last_price: Optional[float] = None

        #: instrument_id -> raw symbol, filled from SymbolMappingMsg records
        self._symbols: dict = {}
        #: the one contract to accept; None until `lock_contract` is called
        self._contract: Optional[str] = None
        #: the last close accepted as real, for the sanity band
        self._reference: Optional[float] = None
        #: timestamp of the last bar handed on, so the series can only move
        #: forward — see the out-of-order guard in `_to_bar`
        self._last_ts: Optional[datetime] = None
        #: counters so the journal can report what was filtered, once, rather
        #: than a line per dropped record
        self._dropped = {"spread": 0, "other_contract": 0, "unidentified": 0,
                         "bad_price": 0, "out_of_band": 0, "out_of_order": 0}
        self._warned_unidentified = False
        self._accepted = 0

    # ------------------------------------------------------------------
    def lock_contract(self, symbol: Optional[str],
                      reference_price: Optional[float] = None,
                      last_time: Optional[datetime] = None) -> None:
        """Accept bars from this contract and no other.

        Called by `LiveTrader.warmup` with the front month the history loader
        selected, so the live stream continues the very series the range was
        built from. Without it the feed would accept every outright under the
        parent symbol at once.
        """
        self._contract = str(symbol).strip().upper() if symbol else None
        if reference_price:
            self._reference = float(reference_price)
        if last_time is not None:
            # the warm-up already covered up to here, so a live record at or
            # before it is a repeat of history, not new information
            self._last_ts = last_time
        if self.log and self._contract:
            self.log.info(f"Live feed locked to contract {self._contract} — "
                          f"bars from any other instrument under "
                          f"{self.cfg.symbols} are ignored.")

    def filter_report(self) -> str:
        """One line describing what the feed rejected. For the shutdown log."""
        dropped = {k: v for k, v in self._dropped.items() if v}
        if not dropped:
            return f"Live feed: {self._accepted:,} bars accepted, none filtered."
        detail = ", ".join(f"{v:,} {k.replace('_', ' ')}" for k, v in dropped.items())
        return f"Live feed: {self._accepted:,} bars accepted | filtered {detail}"

    # ------------------------------------------------------------------
    def start(self) -> None:
        cfg = self.cfg
        self._client = self._db.Live(key=cfg.api_key)
        self._client.subscribe(
            dataset=cfg.dataset,
            schema=cfg.schema,
            stype_in=cfg.stype_in,
            symbols=cfg.symbols,
        )
        if self.log:
            self.log.info(f"Databento live: {cfg.dataset} | {cfg.symbols} "
                          f"| {cfg.schema} | stype_in={cfg.stype_in}")
        self._thread = threading.Thread(target=self._run, name="dbn-live", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            for record in self._client:
                if self._stop.is_set():
                    break
                bar = self._to_bar(record)
                if bar is not None:
                    self._queue.put(bar)
        except Exception as exc:  # pragma: no cover
            if self.log:
                self.log.error(f"Databento live feed stopped: {exc!r}")
        finally:
            self._queue.put(None)

    # ------------------------------------------------------------------
    def _remember_mapping(self, record) -> bool:
        """Capture a SymbolMappingMsg. Returns True if that is what this was.

        Databento emits one per instrument at subscription and again on a roll.
        They are the only thing that turns an `instrument_id` into a symbol we
        can reason about, so they are recorded rather than skipped as noise.
        """
        raw = (getattr(record, "stype_out_symbol", None)
               or getattr(record, "raw_symbol", None))
        iid = getattr(record, "instrument_id", None)
        if raw is None or iid is None:
            return False
        if any(hasattr(record, a) for a in ("open", "close")):
            return False                      # an OHLCV bar, not a mapping
        self._symbols[int(iid)] = str(raw).strip().upper()
        return True

    def _symbol_of(self, record) -> Optional[str]:
        """The instrument this record belongs to, or None if unknowable."""
        direct = (getattr(record, "symbol", None)
                  or getattr(record, "raw_symbol", None))
        if direct:
            return str(direct).strip().upper()
        iid = getattr(record, "instrument_id", None)
        if iid is None:
            return None
        return self._symbols.get(int(iid))

    def _drop(self, reason: str, detail: str = "") -> None:
        self._dropped[reason] = self._dropped.get(reason, 0) + 1
        # First one of each kind is worth a line; after that just count, or a
        # busy parent symbol would bury the journal.
        if self.log and self._dropped[reason] == 1:
            self.log.info(f"Live feed: ignoring {reason.replace('_', ' ')} "
                          f"records{(' — ' + detail) if detail else ''}. "
                          f"Further ones are counted, not logged.")

    def _to_bar(self, record) -> Optional[Bar]:
        # symbol mappings first — they are what makes the filtering below work
        if self._remember_mapping(record):
            return None
        # OHLCVMsg only; ignore system / error records
        if not all(hasattr(record, a) for a in ("open", "high", "low", "close")):
            return None
        ts = getattr(record, "ts_event", None)
        if ts is None:
            return None
        if isinstance(ts, int):
            ts = datetime.fromtimestamp(ts / 1e9, tz=timezone.utc)
        elif ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        # --- which instrument is this? ---------------------------------
        symbol = self._symbol_of(record)
        if symbol is None:
            # Never trade a bar we cannot name. This is loud once, because it
            # means the whole feed is unusable rather than one record.
            self._dropped["unidentified"] += 1
            if self.log and not self._warned_unidentified:
                self._warned_unidentified = True
                self.log.error(
                    "Live feed: a bar arrived with no resolvable symbol, so it "
                    "cannot be told apart from a calendar spread or another "
                    "contract. Dropping it and every one like it. If NO bars "
                    "arrive, the symbol mapping records are missing — check "
                    "the databento client version and stype_in.")
            return None
        if is_spread(symbol):
            # A spread quotes a price DIFFERENCE and is often negative.
            self._drop("spread", f"e.g. {symbol}")
            return None
        if self._contract and symbol != self._contract:
            self._drop("other_contract",
                       f"e.g. {symbol}, keeping {self._contract}")
            return None

        def px(v):
            return float(v) * _PRICE_SCALE if isinstance(v, int) else float(v)

        o, h, l, c = px(record.open), px(record.high), px(record.low), px(record.close)

        # --- backstops, independent of the symbol filtering above -------
        if not all(v > 0 for v in (o, h, l, c)):
            self._drop("bad_price", f"{symbol} closed at {c:,.3f}")
            return None
        if self._reference and abs(c - self._reference) > SANITY_BAND * self._reference:
            self._drop("out_of_band",
                       f"{symbol} at {c:,.3f} vs {self._reference:,.3f}")
            return None

        # --- the series may only move forward ---------------------------
        # `Resampler.push` treats ANY change of bucket as "a new bar", including
        # a bucket in the past: a stale record closes the bar being built and
        # rewinds to the older bucket. Verified — feeding 03:05 after 03:15 into
        # an M15 resampler closes the 03:15 bar early and sets the current
        # bucket back to 03:00.
        #
        # The backtest cannot hit this: `load_dbn_bars` sorts, and de-duplicates
        # on (timestamp, symbol). A live stream offers no such guarantee — a
        # reconnect, a snapshot replay, or the seam between the warm-up and the
        # first live record can all deliver one. So it is enforced here, where
        # it cannot affect the backtest path.
        server_time = self.clock.to_server(ts)
        if self._last_ts is not None and server_time <= self._last_ts:
            self._drop("out_of_order",
                       f"{symbol} at {server_time:%Y-%m-%d %H:%M}, not after "
                       f"{self._last_ts:%Y-%m-%d %H:%M}")
            return None

        self._accepted += 1
        self._reference = c
        self._last_ts = server_time
        self.last_price = c
        return Bar(server_time, o, h, l, c,
                   float(getattr(record, "volume", 0) or 0))

    # ------------------------------------------------------------------
    def poll(self, timeout: float = 1.0) -> Optional[Bar]:
        """Return the next completed base bar, or None if `timeout` elapsed."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._client is not None:
                self._client.stop()
        except Exception:
            pass
