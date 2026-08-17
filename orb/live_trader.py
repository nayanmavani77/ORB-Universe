"""Live trading: Databento market data + MetaTrader 5 execution.

This module contains NO trading logic and no tick sequencing. It only:

  * pulls bars off the Databento live feed,
  * hands them to `Engine` — the same object the backtester drives,
  * journals exits that happened on the broker's server.

Everything that decides a trade lives in `orb.engine` / `orb.strategy`.
"""
from __future__ import annotations

import signal
from datetime import datetime, timezone
from typing import Optional

from .broker import MT5Broker
from .config import AppConfig
from .data.live import DatabentoLiveFeed
from .engine import Engine, MultiEngine  # noqa: F401
from .logger import RbeaLogger, parse_log_level
from .timeutils import ServerClock


class LiveTrader:
    def __init__(self, cfg: AppConfig, logger: Optional[RbeaLogger] = None,
                 broker=None, feed=None, clock: Optional[ServerClock] = None):
        self.cfg = cfg
        self.log = logger or RbeaLogger(
            level=parse_log_level(cfg.strategy.log_level),
            file_path=cfg.strategy.log_file,
            show_time=cfg.strategy.log_show_time,
        )
        self.clock = clock or ServerClock(
            utc_offset_hours=cfg.server_utc_offset_hours,
            timezone_name=cfg.server_timezone)
        # broker / feed are injectable so the live path can be replayed in tests
        self.broker = broker or MT5Broker(cfg.mt5, cfg.symbol,
                                          cfg.strategy.magic, self.log)
        # one strategy per enabled session, exactly as in the backtest — the
        # single-session case yields one engine and behaves as it always did
        cfg.validate_sessions()
        self.engine = MultiEngine(cfg.enabled_sessions(), self.broker,
                                  logger=self.log)
        self.engine.after_tick = self._after_tick
        self.feed = feed or DatabentoLiveFeed(cfg.databento, self.clock, self.log)
        self._running = False
        self._last_deal_check = datetime.now(timezone.utc)

    # convenience
    @property
    def strategy(self):
        return self.engine.strategy

    # ------------------------------------------------------------------
    def warmup(self, days: int = 3) -> None:
        """Seed the bar history from Databento so the range for the current
        session can be rebuilt immediately after a restart."""
        try:
            import databento as db
        except ImportError:
            self.log.warn("databento not installed - skipping history warm-up.")
            return
        import os
        import tempfile
        cfg = self.cfg.databento
        try:
            client = db.Historical(cfg.api_key)
            end = datetime.now(timezone.utc)
            start = datetime.fromtimestamp(end.timestamp() - days * 86400,
                                           tz=timezone.utc)
            data = client.timeseries.get_range(
                dataset=cfg.dataset, symbols=cfg.symbols, schema=cfg.schema,
                stype_in=cfg.stype_in, start=start, end=end)
            fd, tmp = tempfile.mkstemp(suffix=".dbn.zst")
            os.close(fd)
            data.to_file(tmp)
            from .data.dbn import load_dbn_bars
            bars = load_dbn_bars(tmp, self.clock)
            os.unlink(tmp)
            # replay warm-up bars through the engine's resampler only — no
            # trading decisions are taken on history
            for b in bars:
                closed = self.engine.resampler.push(b)
                if closed is not None:
                    self.engine.strategy.ingest_bar(closed)
            if bars:
                self.log.info(f"Warm-up: loaded {len(bars)} historical bars up to "
                              f"{bars[-1].time:%Y.%m.%d %H:%M} server time.")
        except Exception as exc:
            self.log.warn(f"History warm-up failed ({exc!r}) - the EA will build "
                          f"the range from live bars only.")

    # ------------------------------------------------------------------
    def _after_tick(self, now: datetime) -> None:
        """Journal positions that closed on the server (SL / TP / manual)."""
        mt5 = getattr(self.broker, "mt5", None)
        if mt5 is None:                       # simulated broker: nothing to poll
            return
        wall = datetime.now(timezone.utc)
        deals = mt5.history_deals_get(self._last_deal_check, wall) or []
        self._last_deal_check = wall
        reasons = {
            getattr(mt5, "DEAL_REASON_SL", 4): "STOP LOSS hit",
            getattr(mt5, "DEAL_REASON_TP", 5): "TAKE PROFIT hit",
            getattr(mt5, "DEAL_REASON_EXPERT", 3): "closed by EA",
            getattr(mt5, "DEAL_REASON_CLIENT", 0): "closed manually",
            getattr(mt5, "DEAL_REASON_MOBILE", 1): "closed manually",
            getattr(mt5, "DEAL_REASON_WEB", 2): "closed manually",
            getattr(mt5, "DEAL_REASON_SO", 6): "STOP OUT",
        }
        for d in deals:
            if d.magic != self.cfg.strategy.magic:
                continue
            if d.symbol != self.cfg.mt5.symbol:
                continue
            if d.entry != mt5.DEAL_ENTRY_OUT:
                continue                       # opens are journaled on placement
            self.strategy.report_exit(d.position_id, reasons.get(d.reason, "closed"),
                                      d.price, d.profit + d.swap + d.commission,
                                      self.cfg.symbol.currency)

    # ------------------------------------------------------------------
    def run(self, poll_seconds: float = 1.0, max_polls: Optional[int] = None,
            now_fn=None) -> None:
        """Main loop.

        `max_polls` and `now_fn` exist so the identical code path can be
        replayed deterministically in tests.
        """
        self._running = True
        now_fn = now_fn or (lambda bar=None: self.clock.now())

        try:
            signal.signal(signal.SIGINT, lambda *_: self._shutdown())
        except (ValueError, AttributeError):   # not the main thread
            pass

        self.log.info("Times are broker server time. Current server time: "
                      f"{now_fn():%Y.%m.%d %H:%M}")
        self.feed.start()

        polls = 0
        try:
            while self._running:
                if max_polls is not None and polls >= max_polls:
                    break
                polls += 1
                bar = self.feed.poll(timeout=poll_seconds)
                if bar is None:
                    self.engine.on_idle(now_fn())
                else:
                    self.engine.on_bar(bar, now=now_fn(bar))
        finally:
            self.feed.stop()
            if hasattr(self.broker, "shutdown"):
                self.broker.shutdown()
            self.log.info("Stopped: EA removed. Open positions are left untouched.")

    def _shutdown(self) -> None:
        self.log.info("Shutdown requested - open positions are left untouched.")
        self._running = False
