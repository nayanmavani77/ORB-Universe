"""Databento Live feed.

Subscribes to an OHLCV schema and yields completed base bars, converted to
broker/server time.  Records arrive on a background thread and are handed to
the trading loop through a queue, so the loop can keep ticking (for stop-time
handling) even when the market is quiet.
"""
from __future__ import annotations

import queue
import threading
from datetime import datetime, timezone
from typing import Iterator, Optional, Tuple

from ..bars import Bar
from ..timeutils import ServerClock

_PRICE_SCALE = 1e-9


class DatabentoLiveFeed:
    def __init__(self, cfg, clock: ServerClock, logger=None):
        try:
            import databento as db
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("pip install databento") from exc
        if not cfg.api_key:
            raise RuntimeError("No Databento API key. Set `databento.api_key` or the "
                               "DATABENTO_API_KEY environment variable.")
        self._db = db
        self.cfg = cfg
        self.clock = clock
        self.log = logger
        self._queue: "queue.Queue[Optional[Bar]]" = queue.Queue()
        self._client = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.last_price: Optional[float] = None

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

    def _to_bar(self, record) -> Optional[Bar]:
        # OHLCVMsg only; ignore system / symbol-mapping / error records
        if not all(hasattr(record, a) for a in ("open", "high", "low", "close")):
            return None
        ts = getattr(record, "ts_event", None)
        if ts is None:
            return None
        if isinstance(ts, int):
            ts = datetime.fromtimestamp(ts / 1e9, tz=timezone.utc)
        elif ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        def px(v):
            return float(v) * _PRICE_SCALE if isinstance(v, int) else float(v)

        o, h, l, c = px(record.open), px(record.high), px(record.low), px(record.close)
        self.last_price = c
        return Bar(self.clock.to_server(ts), o, h, l, c,
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
