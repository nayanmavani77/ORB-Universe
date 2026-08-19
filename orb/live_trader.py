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
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from .broker import MT5Broker
from .config import AppConfig, journal_settings
from .data.live import DatabentoLiveFeed
from .engine import Engine, MultiEngine  # noqa: F401
from .logger import RbeaLogger
from .timeutils import ServerClock


#: how long one bar of each Databento OHLCV schema covers. Used to tell the
#: engine when an arriving bar finishes its timeframe bucket, so the decision
#: can be taken immediately instead of after a safety wait.
_SCHEMA_SECONDS = {
    "ohlcv-1s": 1, "ohlcv-1m": 60, "ohlcv-1h": 3600, "ohlcv-1d": 86400,
}


def _schema_seconds(schema) -> Optional[int]:
    """Bar length for a Databento schema, or None if it is not a bar schema.
    None is safe: the engine then learns the spacing from the feed itself."""
    return _SCHEMA_SECONDS.get(str(schema or "").strip().lower())


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
        # Tell the broker about every instrument BEFORE any session opens:
        # which terminal symbol each trades, and its contract details read
        # from MT5 itself. Without this an ES order would be sent to gold's
        # symbol and priced with gold's contract size.
        register = getattr(self.broker, "add_instrument", None)
        if callable(register) and cfg.instruments:
            for name, inst in cfg.instruments.items():
                register(name, inst.spec(cfg.symbol),
                         inst.mt5 or cfg.mt5.symbol)

        self.engine = MultiEngine(cfg.enabled_sessions(), self.broker,
                                  logger=self.log)
        self.engine.after_tick = self._after_tick
        # ONE FEED PER INSTRUMENT. Two instruments are two Databento
        # subscriptions with two front months and two price scales; a single
        # stream would merge them exactly as the calendar-spread bug did.
        # A single-instrument run keeps one feed under the "" key, so nothing
        # about it changes.
        self.feeds: Dict[str, object] = {}
        if feed is not None:
            self.feeds[""] = feed
        elif cfg.instruments:
            for name in cfg.instruments:
                self.feeds[name] = DatabentoLiveFeed(
                    self.databento_for(name), self.clock, self.log,
                    instrument=name)
        else:
            self.feeds[""] = DatabentoLiveFeed(cfg.databento, self.clock,
                                               self.log)
        self.feed = next(iter(self.feeds.values()))
        #: which feed gets polled first next time — see `_next_bar`
        self._feed_turn = 0
        self._running = False
        #: throttle for the closed-deal poll; None means "poll now"
        self._last_deal_check = None
        #: deal tickets already journalled, so a wide window cannot repeat one
        self._seen_deals: set = set()
        #: the first poll only records what already existed
        self._deals_primed = False
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

    def databento_for(self, instrument: str):
        """This instrument's Databento settings: the shared block, with the
        instrument's own symbol and any per-instrument overrides applied."""
        import copy
        cfg = copy.deepcopy(self.cfg.databento)
        inst = (self.cfg.instruments or {}).get(instrument)
        if inst is None:
            return cfg
        if inst.signal:
            cfg.symbols = inst.signal
        for attr in ("dataset", "stype_in", "contract_mode", "contract_symbol"):
            value = getattr(inst, attr, "")
            if value:
                setattr(cfg, attr, value)
        return cfg

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
        # Only THIS session's instrument. The warm-up holds every instrument's
        # history merged into one list, so replaying it whole would price a
        # gold session on ES bars and count trades that were never its own.
        want = (getattr(session_cfg, "instrument", "") or "")
        history = [b for b in (self._warmup_bars or [])
                   if (getattr(b, "instrument", "") or "") == want]
        window = [b for b in history if session_start <= b.time < session_end]
        traded = [b for b in history if b.time >= session_end]
        # ONE bar is a perfectly valid range window and must not be rejected:
        # London's 03:00-03:15 on M15 is exactly one bar, as is any 1-minute
        # window on M1. Requiring two silently returned None for precisely
        # those configurations, so the count fell back to MT5 alone — which is
        # blind to trades the EA would have taken while it was not running.
        if not window or not traded:
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

    def _lock_feed(self, contract, bars, instrument: str = "") -> None:
        """Point ONE instrument's live feed at one contract, with a price to
        sanity-check.

        Guarded with `getattr` because tests inject simple fake feeds that have
        no such method, and a missing filter must not break the loop.
        """
        feed = self.feeds.get(instrument, self.feed)
        lock = getattr(feed, "lock_contract", None)
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
                    f"Coverage gap{f' [{instrument}]' if instrument else ''}: "
                    f"history ends {last.time:%H:%M} server "
                    f"time, the live feed starts now — about {gap:,.0f} minute"
                    f"{'s' if gap >= 2 else ''} of bars belong to neither and "
                    f"are lost. Harmless once a range is already built; if a "
                    f"range window falls inside the gap it will be built from "
                    f"fewer bars than usual.")

    def _download_history(self, client, dbn, start, end):
        """One Databento pull, written to a temp file and read back as bars.

        Returns `(contract, bars)`. Kept separate because the warm-up does this
        twice — once normally, once after a 422 tells us where the data really
        ends — and the two copies had already drifted apart once.
        """
        import os
        import tempfile
        data = client.timeseries.get_range(
            dataset=dbn.dataset, symbols=dbn.symbols, schema=dbn.schema,
            stype_in=dbn.stype_in, start=start, end=end)
        fd, tmp = tempfile.mkstemp(suffix=".dbn.zst")
        os.close(fd)
        data.to_file(tmp)
        try:
            from .data.dbn import load_dbn_bars
            contract = self._front_month(tmp)
            bars = load_dbn_bars(tmp, self.clock)
        finally:
            os.unlink(tmp)
        return contract, bars

    def _warmup_one(self, client, instrument: str, days: int) -> list:
        """Warm ONE instrument: download its own history, tag it, feed it to
        the engine, and point that instrument's feed at the same contract.

        Every instrument is a different Databento symbol with its own front
        month and its own price scale, so this cannot be shared. The bars are
        tagged before they reach the engine, which is what stops a gold bar
        from moving an ES range.
        """
        dbn = self.databento_for(instrument)
        end = self._history_end(client, dbn)
        start = datetime.fromtimestamp(end.timestamp() - days * 86400,
                                       tz=timezone.utc)
        try:
            contract, bars = self._download_history(client, dbn, start, end)
        except (AttributeError, TypeError, NameError):
            # a wiring mistake in this module, not a bad network day — never
            # let it hide behind a warning that reads like a Databento outage
            raise
        except Exception as exc:
            # One retry, using the boundary the rejection itself names. This
            # catches the case where the metadata endpoint disagreed with the
            # timeseries endpoint, or was unreachable above.
            capped = self._parse_available_end(exc)
            if capped is None:
                raise
            self.log.warn(f"History warm-up rejected for "
                          f"{instrument or dbn.symbols}: data ends "
                          f"{capped:%Y-%m-%d %H:%M} UTC. Retrying up to there.")
            start = datetime.fromtimestamp(capped.timestamp() - days * 86400,
                                           tz=timezone.utc)
            contract, bars = self._download_history(client, dbn, start, capped)

        for b in bars:
            b.instrument = instrument
        # history only — no trading decisions are taken on past bars
        # (see Engine.warmup_bar)
        for b in bars:
            self.engine.warmup_bar(b)
        if bars:
            self.log.info(f"Warm-up{f' [{instrument}]' if instrument else ''}: "
                          f"loaded {len(bars)} historical bars up to "
                          f"{bars[-1].time:%Y.%m.%d %H:%M} server time.")
        self._lock_feed(contract, bars, instrument)
        return bars

    def warmup(self, days: int = 3) -> None:
        """Seed the bar history from Databento so the range for the current
        session can be rebuilt immediately after a restart.

        With several instruments this runs once per instrument. One failing
        does not stop the others: a warning is logged and that instrument
        starts from live bars only, exactly as a single-instrument run does
        when its download fails.
        """
        try:
            import databento as db
        except ImportError:
            self.log.warn("databento not installed - skipping history warm-up.")
            return
        client = db.Historical(self.cfg.databento.api_key)
        names = list(self.cfg.instruments or {}) or [""]
        collected = []
        for name in names:
            try:
                collected.extend(self._warmup_one(client, name, days))
            except (AttributeError, TypeError, NameError):
                raise
            except Exception as exc:
                self.log.warn(
                    f"History warm-up failed"
                    f"{f' for {name}' if name else ''} ({exc!r}) - the EA will "
                    f"build the range from live bars only. A session whose "
                    f"range window has already closed will be SKIPPED for "
                    f"today.")
        # merged the same way the backtest merges them, so a replay sees the
        # instruments interleaved exactly as `load_instrument_bars` would
        collected.sort(key=lambda b: (b.time, getattr(b, "instrument", "") or ""))
        self._warmup_bars = collected
        self.check_price_scale()

    # ------------------------------------------------------------------
    #: How far the signal price and the execution price may sit apart before
    #: it stops looking like basis and starts looking like a mistake.
    #:
    #: Real basis is small: GC futures and XAUUSD spot sit about $56 apart on
    #: ~2400, which is 2.3%. Index CFDs track their futures within a fraction
    #: of a percent. So 10% is comfortably above any honest basis...
    SCALE_WARN = 0.10
    #: ...and beyond this the two are not the same market on the same scale.
    #: A CFD quoted in a different unit — 608.0 where the future says 6080 —
    #: lands here, and so does a symbol mapped to the wrong market entirely.
    SCALE_ALARM = 0.50

    def check_price_scale(self) -> None:
        """Prove the signal and the execution venue are quoted on ONE scale.

        This is the assumption the whole cross-instrument design rests on and
        the only one nothing else checks. The strategy computes its range, stop
        and target from Databento FUTURES prices, then sends the stop and
        target to MT5 as DISTANCES IN POINTS (see `translate_levels`). That is
        only valid if one point means the same size of move on both sides.

        It usually does — ES and a US500 CFD are both quoted in index points,
        GC and XAUUSD both in dollars per ounce — but "usually" is not a thing
        to trade on. If a broker quotes its CFD on a different scale, every
        stop and target is wrong by that factor, silently: the orders are
        accepted, the journal looks ordinary, and only the money is wrong.

        So compare the two prices directly, once, at start-up. Nothing is
        blocked — a stale weekend quote should not stop a session — but the
        operator gets a loud, specific warning before the first order.
        """
        last = {}
        for b in self._warmup_bars or []:
            last[getattr(b, "instrument", "") or ""] = b
        if not last:
            return

        for name, bar in sorted(last.items()):
            try:
                quote = self.broker.ask(name) if name else self.broker.ask()
            except Exception:
                continue
            signal = float(bar.close)
            if not quote or not signal:
                continue                     # market closed, or a sim broker

            gap = abs(quote - signal) / signal
            who = f"[{name}] " if name else ""
            symbol = getattr(self.broker, "symbol_for", lambda _n: "")(name) \
                or getattr(self.broker, "symbol", "")
            detail = (f"signal {signal:,.2f} vs {symbol or 'broker'} "
                      f"{quote:,.2f} ({gap * 100:,.1f}% apart)")

            if gap >= self.SCALE_ALARM:
                ratio = quote / signal
                self.log.warn(
                    f"{who}PRICE SCALE MISMATCH — {detail}. These do not look "
                    f"like the same market on the same scale"
                    + (f" (roughly {ratio:.3g}x)" if ratio else "") + ". Stops "
                    f"and targets cross as DISTANCES IN POINTS, so if one "
                    f"point means a different size of move on each side, every "
                    f"stop and target is wrong by that factor and nothing else "
                    f"will tell you. CHECK the `mt5:` symbol for this "
                    f"instrument before letting it trade.")
            elif gap >= self.SCALE_WARN:
                self.log.warn(
                    f"{who}Wide basis — {detail}. Larger than the usual gap "
                    f"between a future and its cash/CFD equivalent. Worth "
                    f"confirming the mapping is the market you meant.")
            else:
                self.log.info(f"{who}Scale check OK — {detail}.")

    # ------------------------------------------------------------------
    #: how often the closed-deal poll actually queries MT5
    DEAL_POLL_SECONDS = 5.0
    #: how far either side of the broker's clock to look for closed deals.
    #: Wide on purpose — see `_after_tick`.
    DEAL_WINDOW_DAYS = 2

    def _after_tick(self, now: datetime) -> None:
        """Journal positions that closed on the server (SL / TP / manual).

        This used to query a MOVING window, `[last_check, wall]`, built from
        `datetime.now(timezone.utc)`. Two faults, and together they lost exits.

        1. MT5 reports deal times in the BROKER's server time, not UTC. On a
           server at UTC+3 the window was three hours in the past, so a deal
           that had just happened fell outside it.
        2. `last_check` advanced whether or not anything was found, so a deal
           the window missed was missed FOREVER.

        Seen live on 2026-08-18: a take-profit on ticket #4549601521 never
        produced an EXIT line, while a stop-loss eight minutes later did. The
        EA's trading was unaffected — `positions_count` reads positions
        directly, and it correctly re-armed once flat — but the journal, which
        is the only record of what the account did, silently lost a trade.

        Now: a deliberately WIDE window anchored on the broker's own clock, and
        de-duplication by deal ticket. Nothing is lost by being early or late,
        and nothing is reported twice. The first poll only PRIMES the seen-set,
        so deals from before the EA started are not announced as if they had
        just happened.
        """
        mt5 = getattr(self.broker, "mt5", None)
        if mt5 is None:                       # simulated broker: nothing to poll
            return
        wall = datetime.now(timezone.utc)
        if (self._last_deal_check is not None
                and (wall - self._last_deal_check).total_seconds()
                < self.DEAL_POLL_SECONDS):
            return                            # throttle: the window is wide
        self._last_deal_check = wall

        span = timedelta(days=self.DEAL_WINDOW_DAYS)
        anchor = self.broker.server_time()
        deals = mt5.history_deals_get(anchor - span, anchor + span) or []

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
        first_pass = not self._deals_primed
        self._deals_primed = True

        for d in deals:
            ticket = getattr(d, "ticket", None)
            if ticket is None or ticket in self._seen_deals:
                continue
            self._seen_deals.add(ticket)
            if first_pass:
                continue                       # priming only: already history
            if owns is not None and not owns(d.magic):
                continue
            if d.symbol not in self._our_symbols():
                continue
            if d.entry != mt5.DEAL_ENTRY_OUT:
                continue                       # opens are journaled on placement
            # Report through the strategy that OPENED it, not `self.strategy`
            # — which is the first session's, and therefore wrong the moment a
            # run has more than one. `orb/backtest.py` already routes exits by
            # session name; live has the magic, which names the session
            # uniquely (`runconfig.merge` refuses a run whose magics collide).
            self._strategy_for_magic(d.magic).report_exit(
                d.position_id, reasons.get(d.reason, "closed"),
                d.price, d.profit + d.swap + d.commission,
                self.cfg.symbol.currency)

    def _our_symbols(self) -> set:
        """Every terminal symbol this run trades. A portfolio account may hold
        several, and filtering on one would drop the others' exits."""
        symbols = {self.cfg.mt5.symbol}
        for inst in (self.cfg.instruments or {}).values():
            if inst.mt5:
                symbols.add(inst.mt5)
        return symbols

    def _strategy_for_magic(self, magic: int):
        """The strategy of the session that uses this magic number.

        Falls back to the first session when the magic is unknown, which keeps
        a single-session run behaving exactly as it always did.
        """
        for engine in getattr(self.engine, "engines", [self.engine]):
            if int(getattr(engine.cfg, "magic", -1)) == int(magic):
                return engine.strategy
        return self.strategy

    # ------------------------------------------------------------------
    def _next_bar(self, poll_seconds: float):
        """The next bar from any feed, fairly.

        ONE feed — every single-instrument run — is polled exactly as it always
        was: one blocking poll, nothing else changed.

        With several, a naive `for f in feeds: poll(blocking)` is wrong twice
        over. It always starts at the same feed, so a busy first instrument can
        starve a quiet second one; and each blocking poll costs its full
        timeout, so N feeds make one loop pass take N x poll_seconds and the
        idle tick — which drives stop times and range building — runs N times
        slower.

        So: sweep every feed without blocking first, starting one further along
        each time, and only block when they are all empty. Whichever feed
        answers, the bar arrives tagged and `MultiEngine` routes it.
        """
        feeds = list(self.feeds.values())
        if len(feeds) == 1:
            return feeds[0].poll(timeout=poll_seconds)

        n = len(feeds)
        for i in range(n):
            f = feeds[(self._feed_turn + i) % n]
            bar = f.poll(timeout=0)
            if bar is not None:
                self._feed_turn = (self._feed_turn + i + 1) % n
                return bar
        # nothing waiting anywhere: block once, on the feed whose turn it is,
        # so the loop sleeps rather than spinning the CPU
        f = feeds[self._feed_turn]
        self._feed_turn = (self._feed_turn + 1) % n
        return f.poll(timeout=poll_seconds)

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
            # The minutes neither the warm-up download nor the live feed
            # covers. `None` for the start means there was no warm-up at all,
            # so everything before now is missing. A range window overlapping
            # this cannot be trusted — see `_range_window_has_a_hole`.
            # Act the instant a bar is finished, not a grace period later —
            # but ONLY if the broker can price a decision at that instant.
            # A bar-priced broker (SimBroker) fills at the open of the bar the
            # EA reacted on, so it cannot price anything until the NEXT bar
            # exists; closing early there would fill at a price a whole bar
            # stale. `tests/test_single_source.py` drives this very path with a
            # SimBroker to prove live and backtest agree trade-for-trade, and
            # that proof is worth more than shaving seconds off a simulation.
            engine.eager_close = not getattr(self.broker, "prices_from_bars", True)
            engine.base_seconds = _schema_seconds(self.cfg.databento.schema)
            engine.strategy.coverage_gap = (
                self._warmup_bars[-1].time if self._warmup_bars else None,
                started_at)

        try:
            signal.signal(signal.SIGINT, lambda *_: self._shutdown())
        except (ValueError, AttributeError):   # not the main thread
            pass

        self.log.info("Times are broker server time. Current server time: "
                      f"{now_fn():%Y.%m.%d %H:%M}")
        for f in self.feeds.values():
            f.start()

        polls = 0
        try:
            while self._running:
                if max_polls is not None and polls >= max_polls:
                    break
                polls += 1
                bar = self._next_bar(poll_seconds)
                if bar is None:
                    self.engine.on_idle(now_fn())
                else:
                    self.engine.on_bar(bar, now=now_fn(bar))
        finally:
            for f in self.feeds.values():
                f.stop()
            # what the feed filtered out — worth seeing, because a large
            # "other contract" count next to zero accepted means the lock is
            # on the wrong symbol and the EA has been sitting blind
            for f in self.feeds.values():
                report = getattr(f, "filter_report", None)
                if callable(report):
                    self.log.info(report())
            if hasattr(self.broker, "shutdown"):
                self.broker.shutdown()
            self.log.info("Stopped: EA removed. Open positions are left untouched.")

    def _shutdown(self) -> None:
        self.log.info("Shutdown requested - open positions are left untouched.")
        self._running = False
