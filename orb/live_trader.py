"""Live trading: Databento market data + MetaTrader 5 execution.

This module contains NO trading logic and no tick sequencing. It only:

  * pulls bars off the Databento live feed,
  * hands them to `MultiEngine` — the same object the backtester drives,
  * journals exits that happened on the broker's server.

Everything that decides a trade lives in the engine a session names:
`orb/engines/<engine>/strategy.py`, reached through `orb.registry`. Because the
lookup is per session, a live account can run several engines side by side.
"""
from __future__ import annotations

import signal
from datetime import datetime, timezone
from typing import Optional

from .broker import MT5Broker
from .config import AppConfig, journal_settings
from .data.live import DatabentoLiveFeed
from .engine import Engine, MultiEngine  # noqa: F401
from .logger import RbeaLogger
from .timeutils import ServerClock


class LiveTrader:
    def __init__(self, cfg: AppConfig, logger: Optional[RbeaLogger] = None,
                 broker=None, feed=None, clock: Optional[ServerClock] = None):
        self.cfg = cfg
        _level, _file, _show_time = journal_settings(cfg)
        self.log = logger or RbeaLogger(level=_level, file_path=_file,
                                        show_time=_show_time)
        self.clock = clock or ServerClock(
            utc_offset_hours=cfg.server_utc_offset_hours,
            timezone_name=cfg.server_timezone)
        # broker / feed are injectable so the live path can be replayed in tests
        cfg.validate_sessions()
        # the broker owns every enabled session's magic: it must recognise all
        # of its own positions, while each order carries its session's own tag
        magics = {s.magic for s in cfg.enabled_sessions()} or {cfg.strategy.magic}
        self.broker = broker or MT5Broker(cfg.mt5, cfg.symbol, magics, self.log)
        # one strategy per enabled session, exactly as in the backtest — the
        # single-session case yields one engine and behaves as it always did
        self.engine = MultiEngine(cfg.enabled_sessions(), self.broker,
                                  logger=self.log)
        self.engine.after_tick = self._after_tick
        self.feed = feed or DatabentoLiveFeed(cfg.databento, self.clock, self.log)
        self._running = False
        self._last_deal_check = datetime.now(timezone.utc)
        #: the history the warm-up downloaded. Kept because it is the only
        #: record of what a session did before the EA was running — see
        #: `_replay_session`.
        self._warmup_bars: list = []

    # convenience
    @property
    def strategy(self):
        return self.engine.strategy

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_available_end(text: str):
        """Pull the available end timestamp out of a Databento 422 message.

        The API says, in prose: *"The dataset GLBX.MDP3 has data available up
        to '2026-08-18 02:30:00+00:00'"*. That sentence is the only place the
        boundary appears, so it is worth reading rather than guessing again.
        """
        import re
        m = re.search(r"available up to '([^']+)'", str(text))
        if not m:
            return None
        try:
            stamp = datetime.fromisoformat(m.group(1))
        except ValueError:
            return None
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)

    def _history_end(self, client, cfg) -> datetime:
        """The latest timestamp the HISTORICAL API will actually serve.

        Historical data lags the live feed — GLBX.MDP3 is typically minutes to
        hours behind. Asking for `end = now` is therefore a request for data
        that does not exist yet, and the API rejects the WHOLE query with a 422
        rather than returning what it has. That killed the warm-up outright:

            data_end_after_available_end — The dataset GLBX.MDP3 has data
            available up to '2026-08-18 02:30:00+00:00'. The `end` in the
            query ('2026-08-18 02:32:39') is after the available range.

        and the EA then started with no history, so any session whose range
        window had already closed was skipped for the day. Ask the metadata
        endpoint where the data actually ends and clamp to it.
        """
        now = datetime.now(timezone.utc)
        try:
            rng = client.metadata.get_dataset_range(dataset=cfg.dataset)
        except Exception as exc:                      # metadata down, or offline
            self.log.warn(f"Could not read the dataset's available range "
                          f"({exc!r}) - requesting history up to now and "
                          f"letting the retry below handle a rejection.")
            return now
        # the field has been spelled several ways across client versions
        raw = None
        if isinstance(rng, dict):
            for key in ("end", "available_end", "end_date"):
                if rng.get(key):
                    raw = rng[key]
                    break
        else:
            raw = getattr(rng, "end", None) or getattr(rng, "available_end", None)
        if raw is None:
            return now
        try:
            available = (raw if isinstance(raw, datetime)
                         else datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
        except ValueError:
            return now
        if available.tzinfo is None:
            available = available.replace(tzinfo=timezone.utc)
        if available < now:
            behind = (now - available).total_seconds() / 60.0
            self.log.info(f"Historical data ends {available:%Y-%m-%d %H:%M} UTC, "
                          f"{behind:,.0f} min behind now - warming up to there. "
                          f"The live feed covers the rest.")
        return min(now, available)

    # ------------------------------------------------------------------
    def _front_month(self, path) -> Optional[str]:
        """The most-traded outright contract in the warm-up history.

        The live stream carries every contract under the parent symbol at once,
        so the feed has to be told which one to follow. The answer is already
        implicit in the history just downloaded: whichever outright carried the
        most volume over the warm-up window is the front month right now, and
        it is the same series `load_dbn_bars` built the warm-up bars from.
        """
        mode = str(getattr(self.cfg.databento, "contract_mode", "") or "").lower()
        fixed = getattr(self.cfg.databento, "contract_symbol", None)
        if mode == "symbol" and fixed:
            return str(fixed).strip().upper()
        if mode == "all":
            self.log.warn(
                "contract_mode is 'all', so the live feed is NOT locked to one "
                "contract. Bars from every outright under "
                f"{self.cfg.databento.symbols} will be interleaved as if they "
                "were one instrument. This is almost certainly wrong for live "
                "trading — use front_month_volume or a fixed --contract.")
            return None
        try:
            from .data.dbn import list_contracts
            table = list_contracts(path)          # spreads already excluded
            if table is None or len(table) == 0:
                return None
            return str(table.index[0]).strip().upper()   # sorted by volume desc
        except Exception as exc:
            self.log.warn(f"Could not identify the front-month contract "
                          f"({exc!r}). The live feed will accept any outright "
                          f"under {self.cfg.databento.symbols}, which mixes "
                          f"contracts — prefer a fixed --contract until this "
                          f"is resolved.")
            return None

    def _replay_session(self, session_start, session_end, session_cfg):
        """How many trades this session ALREADY signalled, before the EA ran.

        The warm-up bars are the session's real history. Replaying them through
        the very same engine — `run_backtest`, `SimBroker`, the identical
        strategy class and settings — reproduces exactly what the backtest
        would have taken. Verified against 193 Asia sessions of 2026 data:
        the replay matched the full backtest's count in 193 of 193.

        This is what lets a late start behave correctly instead of being
        abandoned. A cruder measure (counting range excursions) over-counted by
        roughly 10x — 17 per session against 1.76 real trades — and would have
        declared the allowance spent in 91% of sessions. The replay says the
        true figure is 46%, so more than half of late-joined sessions still
        have room and are safe to trade.

        Returns None if there is nothing to replay, in which case the caller
        falls back to the conservative path.
        """
        import copy
        window = [b for b in self._warmup_bars
                  if session_start <= b.time < session_end] if self._warmup_bars else []
        traded = [b for b in self._warmup_bars
                  if b.time >= session_end] if self._warmup_bars else []
        if len(window) < 2 or not traded:
            return None
        try:
            from .backtest import run_backtest        # local: see note above
            cfg = copy.deepcopy(self.cfg)
            # one session, exactly this one — a replay must not let another
            # session's windows or magic bleed into the count
            name = session_cfg.name
            for s in cfg.sessions.values():
                s.enabled = (s.name == name)
            cfg.backtest.out_dir = None
            result = run_backtest(cfg, window + traded,
                                  logger=RbeaLogger(level=0))
            return len(result.trades)
        except Exception as exc:
            self.log.warn(f"Could not replay this session's history ({exc!r}).")
            return None

    def _lock_feed(self, contract, bars) -> None:
        """Point the live feed at one contract, with a price to sanity-check.

        Guarded with `getattr` because tests inject simple fake feeds that have
        no such method, and a missing filter must not break the loop.
        """
        lock = getattr(self.feed, "lock_contract", None)
        if not callable(lock):
            return
        last = bars[-1] if bars else None
        try:
            lock(contract,
                 last.close if last else None,
                 last_time=last.time if last else None)
        except TypeError:
            # a feed from an older signature, or a test double
            lock(contract, last.close if last else None)

        # The historical API lags the live feed, so the minutes between where
        # history stops and where the subscription starts belong to neither.
        # Small and unavoidable, but the operator should see it rather than
        # wonder later why a bar is missing from a range.
        if last is not None:
            gap = (self.clock.to_server(datetime.now(timezone.utc))
                   - last.time).total_seconds() / 60.0
            if gap > 1.5:
                self.log.warn(
                    f"Coverage gap: history ends {last.time:%H:%M} server "
                    f"time, the live feed starts now — about {gap:,.0f} minute"
                    f"{'s' if gap >= 2 else ''} of bars belong to neither and "
                    f"are lost. Harmless once a range is already built; if a "
                    f"range window falls inside the gap it will be built from "
                    f"fewer bars than usual.")

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
            end = self._history_end(client, cfg)
            start = datetime.fromtimestamp(end.timestamp() - days * 86400,
                                           tz=timezone.utc)
            data = client.timeseries.get_range(
                dataset=cfg.dataset, symbols=cfg.symbols, schema=cfg.schema,
                stype_in=cfg.stype_in, start=start, end=end)
            fd, tmp = tempfile.mkstemp(suffix=".dbn.zst")
            os.close(fd)
            data.to_file(tmp)
            from .data.dbn import load_dbn_bars
            contract = self._front_month(tmp)
            bars = load_dbn_bars(tmp, self.clock)
            os.unlink(tmp)
            # replay warm-up bars into history only — no trading decisions
            # are taken on past bars (see Engine.warmup_bar)
            for b in bars:
                self.engine.warmup_bar(b)
            self._warmup_bars = list(bars)
            if bars:
                self.log.info(f"Warm-up: loaded {len(bars)} historical bars up to "
                              f"{bars[-1].time:%Y.%m.%d %H:%M} server time.")
            self._lock_feed(contract, bars)
        except (AttributeError, TypeError, NameError):
            # a wiring mistake in this module, not a bad network day - never
            # let it hide behind a warning that reads like a Databento outage
            raise
        except Exception as exc:
            # One retry, using the boundary the rejection itself names. This
            # catches the case where the metadata endpoint disagreed with the
            # timeseries endpoint, or was unreachable above.
            capped = self._parse_available_end(exc)
            if capped is not None:
                self.log.warn(f"History warm-up rejected: data ends "
                              f"{capped:%Y-%m-%d %H:%M} UTC. Retrying up to "
                              f"there.")
                try:
                    start = datetime.fromtimestamp(
                        capped.timestamp() - days * 86400, tz=timezone.utc)
                    data = client.timeseries.get_range(
                        dataset=cfg.dataset, symbols=cfg.symbols,
                        schema=cfg.schema, stype_in=cfg.stype_in,
                        start=start, end=capped)
                    fd, tmp = tempfile.mkstemp(suffix=".dbn.zst")
                    os.close(fd)
                    data.to_file(tmp)
                    from .data.dbn import load_dbn_bars
                    contract = self._front_month(tmp)
                    bars = load_dbn_bars(tmp, self.clock)
                    os.unlink(tmp)
                    for b in bars:
                        self.engine.warmup_bar(b)
                    self._warmup_bars = list(bars)
                    if bars:
                        self.log.info(
                            f"Warm-up: loaded {len(bars)} historical bars up to "
                            f"{bars[-1].time:%Y.%m.%d %H:%M} server time.")
                    self._lock_feed(contract, bars)
                    return
                except Exception as retry_exc:
                    exc = retry_exc
            self.log.warn(f"History warm-up failed ({exc!r}) - the EA will build "
                          f"the range from live bars only. A session whose range "
                          f"window has already closed will be SKIPPED for today.")

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
        owns = getattr(self.broker, "owns", None)
        for d in deals:
            if owns is not None and not owns(d.magic):
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

        # Stamp every session's strategy with the moment live trading begins.
        # This is the ONLY place `started_at` is set — `run_backtest` never
        # does — which is what keeps the late-start rules inert in a backtest.
        #
        # A session whose range window closed before this instant is one the
        # EA joined late: it never saw that session's first breakout, and its
        # in-memory trade counter is not the session's real total. Both are
        # corrected in `OrbStrategy._resync_late_session`.
        started_at = now_fn()
        for engine in getattr(self.engine, "engines", [self.engine]):
            engine.strategy.started_at = started_at
            # how the strategy asks "what did this session already signal?".
            # Injected rather than imported so the strategy never depends on
            # the backtest module, and so tests can supply a stub.
            engine.strategy.session_replay = self._replay_session

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
            # what the feed filtered out — worth seeing, because a large
            # "other contract" count next to zero accepted means the lock is
            # on the wrong symbol and the EA has been sitting blind
            report = getattr(self.feed, "filter_report", None)
            if callable(report):
                self.log.info(report())
            if hasattr(self.broker, "shutdown"):
                self.broker.shutdown()
            self.log.info("Stopped: EA removed. Open positions are left untouched.")

    def _shutdown(self) -> None:
        self.log.info("Shutdown requested - open positions are left untouched.")
        self._running = False
