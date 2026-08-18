"""RangeBreakoutEA — strategy core, ported 1:1 from RangeBreakoutEA.mq5 v1.70.

Every rule of the original is preserved:

  1. Range = High/Low of the signal-timeframe bars between Range Start and
     Range End (server time).  Built once, on the first tick at/after the
     window ends.  Mid = (High + Low) / 2.
  2. Entry on a *closed* bar whose close is beyond Range High (BUY) or Range
     Low (SELL).  The bar must have closed after the range window ended.
  3. SL = range midpoint, or the opposite side of the range (full-range mode).
  4. TP = R:R x |real fill price - SL|, applied after the fill via modify.
  5. Arming: the range arms the first breakout.  After a fill the EA disarms;
     with `require_range_reentry` it re-arms only when a bar closes back inside
     the range, otherwise on the first closed bar while flat.
  6. One position at a time, optional max trades per session, News Days.
  7. Trading window runs from Range End to Stop Time (or to the next session's
     range start when Stop Time is disabled); optionally flatten at Stop Time.

The class is deliberately free of any data-source or broker specifics.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from ...bars import Bar, BarStore
from ...broker import Broker, Position
from ...config import (NEWS_OFF, NEWS_ON, NEWS_ONLY, SL_FULL_RANGE,
                     SL_MIDPOINT, StrategyConfig)
from ...logger import RbeaLogger
from ...timeutils import (SECONDS_PER_DAY, NewsDays, date_only, fmt_date,
                        fmt_dt, fmt_time, parse_hhmm, timeframe_seconds)


class OrbStrategy:
    def __init__(self, cfg: StrategyConfig, broker: Broker,
                 store: Optional[BarStore] = None,
                 logger: Optional[RbeaLogger] = None):
        cfg.validate()
        self.cfg = cfg
        self.broker = broker
        self.store = store if store is not None else BarStore()
        self.log = logger or RbeaLogger()

        self.tf_seconds = timeframe_seconds(cfg.signal_timeframe)

        self.start_sec, _ = parse_hhmm(cfg.range_start)
        self.end_sec, _ = parse_hhmm(cfg.range_end)
        self.stop_sec, stop_disabled = parse_hhmm(cfg.stop_time)
        self.stop_enabled = not stop_disabled

        # every news bucket, uniform: (label, dates, mode).  The un-categorised
        # `news_days` / `news_trading` pair is simply a ninth bucket.
        self.news_buckets = []
        for _key, label, cat in cfg.news.items():
            self.news_buckets.append(
                (label, NewsDays(cat.dates, warn=self.log.warn), cat.mode))
        self.news_buckets.append(
            ("News Days (general)", NewsDays(cfg.news_days, warn=self.log.warn),
             cfg.news_trading))
        # buckets with no dates can never match, so they cannot restrict anything
        self.active_buckets = [b for b in self.news_buckets if len(b[1])]
        self.only_buckets = [b for b in self.active_buckets if b[2] == NEWS_ONLY]
        self.off_buckets = [b for b in self.active_buckets if b[2] == NEWS_OFF]

        # --- session state (mirrors the EA globals) ---
        self.session_start: Optional[datetime] = None
        self.session_end: Optional[datetime] = None
        self.trade_until: Optional[datetime] = None
        self.range_computed = False
        self.range_valid = False
        self.range_high = 0.0
        self.range_low = 0.0
        self.range_mid = 0.0
        self.trades_this_session = 0
        self.armed = False
        self.closed_at_stop = False
        self.last_bar_time: Optional[datetime] = None
        #: OPEN time of the last bar filed into history — see `ingest_bar`
        self.last_ingested_time: Optional[datetime] = None

        #: LIVE ONLY. `(from, to)` that neither the warm-up download nor the
        #: live subscription covers — the historical API always lags the live
        #: feed by minutes, and those minutes belong to neither. `from` is None
        #: when there is no warm-up at all, meaning everything before the EA
        #: started is missing. A range window that overlaps this cannot be
        #: trusted; see `_range_window_has_a_hole`.
        self.coverage_gap = None

        #: Server time the EA began running. LIVE ONLY — `run_backtest` never
        #: sets it, so every rule keyed on it is inert in a backtest and the
        #: golden master stays identical. See `_is_late_session`.
        self.started_at: Optional[datetime] = None
        #: set on a late session until price closes back INSIDE the range, so
        #: the EA only ever trades a breakout it witnessed itself
        self._await_reentry = False
        #: set when a late session has no allowance left, or none could be
        #: determined — see `_adopt_late_session`
        self._session_blocked = False

        #: Injected by `LiveTrader.run`: how many trades a session had already
        #: signalled before the EA started, by replaying its own history.
        #: Absent in a backtest, where no session is ever joined late.
        self.session_replay = None

        self._banner()

    # ------------------------------------------------------------------
    # OnInit banner
    # ------------------------------------------------------------------
    def _stop_loss_label(self) -> str:
        """How the stop is described in the startup banner.

        Overridable, because it is a claim about what this engine will actually
        do. The banner used to hard-code the two `sl_mode` values, so a session
        running `orb_reverse` announced "SL mid range" while its stop was in
        fact 0.75 x the range measured from the fill — `sl_mode` is not even
        read by that engine. On a live account that line is the operator's
        confirmation that the config took effect, so it has to be true.
        """
        return "full range" if self.cfg.sl_mode == SL_FULL_RANGE else "mid range"

    def _banner(self) -> None:
        if self.active_buckets:
            self.log.info("News categories:")
            for label, dates, mode in self.news_buckets:
                if not len(dates):
                    continue
                note = {NEWS_ON: "trade these days (no restriction)",
                        NEWS_OFF: "do NOT trade these days",
                        NEWS_ONLY: "trade ONLY these days"}[mode]
                self.log.info(f"   {label:<28} {mode.upper():<5} "
                              f"{len(dates):>3} entr{'y' if len(dates)==1 else 'ies'}"
                              f"  - {note}")
            if self.only_buckets:
                names = ", ".join(b[0] for b in self.only_buckets)
                self.log.info(f"At least one category is ONLY ({names}), so a "
                              f"day must match one of them to be tradeable.")
            if self.off_buckets and self.only_buckets:
                self.log.warn("Both OFF and ONLY categories are set. OFF wins: "
                              "a day listed in an OFF category is never traded, "
                              "even if an ONLY category also lists it.")
        else:
            self.log.info("News categories: none configured - every day is "
                          "tradeable.")
        for label, dates, mode in self.news_buckets:
            if mode == NEWS_ON and len(dates):
                self.log.warn(f"{label} is ON, so its {len(dates)} date(s) have "
                              f"no effect on trading.")
        self.log.info(
            "Started on {sym} | range {a}-{b} | stop {s} | TF {tf} | SL {sl} "
            "| R:R 1:{rr:.2f} | lots {lots:.2f} | max {mx} trade(s)/session".format(
                sym=self.broker.spec.name,
                a=self.cfg.range_start, b=self.cfg.range_end,
                s=(self.cfg.stop_time if self.stop_enabled else "0 (continuous)"),
                tf=self.cfg.signal_timeframe,
                sl=self._stop_loss_label(),
                rr=self.cfg.risk_reward, lots=self.cfg.lots,
                mx=(self.cfg.max_trades_per_session
                    if self.cfg.max_trades_per_session > 0 else "unlimited")))
        self.log.info(
            "Re-entry rule: after a trade closes, price must close back inside the "
            "range before the next breakout is taken."
            if self.cfg.require_range_reentry else
            "Re-entry rule: any breakout close is taken whenever the EA is flat "
            "(no range re-entry required).")
        if not self.cfg.require_range_reentry:
            self.log.warn("Range re-entry is not required - after a trade closes, the "
                          "very next candle closing outside the range will open a new one.")

    # ------------------------------------------------------------------
    # Session handling  (MQL5: SessionTradeUntil / StartNewSession / SyncSession)
    # ------------------------------------------------------------------
    def _session_trade_until(self, s_start: datetime, s_end: datetime) -> datetime:
        next_start = s_start + timedelta(days=1)
        if not self.stop_enabled:
            return next_start                    # runs until the next range starts
        st = date_only(s_end) + timedelta(seconds=self.stop_sec)
        while st <= s_end:
            st += timedelta(days=1)
        return st if st < next_start else next_start

    def _start_new_session(self, session_start: datetime) -> None:
        duration = (self.end_sec - self.start_sec + SECONDS_PER_DAY) % SECONDS_PER_DAY
        self.session_start = session_start
        self.session_end = session_start + timedelta(seconds=duration)
        self.trade_until = self._session_trade_until(self.session_start, self.session_end)
        self.range_computed = False
        self.range_valid = False
        self.range_high = self.range_low = self.range_mid = 0.0
        self.trades_this_session = 0
        self.armed = False
        # cleared per session: both only ever apply to a session that was
        # already under way when the EA started (see `_is_late_session`)
        self._await_reentry = False
        self._session_blocked = False
        self.closed_at_stop = False
        self.log.reset_once_keys()

        self.log.info(
            "New session | range window {a} .. {b} | trading until {c}{d}".format(
                a=fmt_dt(self.session_start), b=fmt_time(self.session_end),
                c=fmt_dt(self.trade_until),
                d=("" if self.stop_enabled else " (no stop time - runs to next range)")))
        allowed, why = self._news_decision(self.session_start)
        if why:
            self.log.info(("Session date: " + why) if allowed else
                          ("Session date: " + why + " - the range is still built "
                           "but no trades will be taken."))

    def _sync_session(self, now: datetime) -> None:
        today_start = date_only(now) + timedelta(seconds=self.start_sec)
        s_start = today_start if now >= today_start else today_start - timedelta(days=1)
        if s_start != self.session_start:
            self._start_new_session(s_start)

    # ------------------------------------------------------------------
    # Range building  (MQL5: ComputeRange)
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def _window_bars_are_in(self) -> bool:
        """Has the LAST timeframe bar of the range window closed and been filed?

        A timeframe bar only closes when the next one starts, so at the exact
        instant the window ends its final bar is still forming. In a backtest
        that is invisible: `_tick` runs only when a bar arrives, and the bar
        that ends the window is ingested immediately before `on_time`. Live,
        `on_idle` ticks every second, so `on_time` reaches this point at the
        stroke of the window end — before the feed has delivered anything.

        The range was then built from an empty store. For a 30-minute M1 window
        that quietly lost the last bar; for London's 15-minute window on M15 —
        exactly ONE bar — it lost the only one, and the session was skipped
        every single day with "No bars inside range window". Reproduced by
        driving the live loop through a full window with bars flowing normally.

        `last_bar_time` is the OPEN time of the most recently filed bar, so the
        window's final bar has arrived once it reaches `session_end - one bar`.
        """
        if self.last_ingested_time is None:
            return False
        return self.last_ingested_time >= (self.session_end
                                           - timedelta(seconds=self.tf_seconds))

    def _warn_waiting_for_window(self, now: datetime) -> None:
        """Say so if the wait above becomes unusually long.

        Normally it lasts until the next bar arrives — seconds. A long wait
        means the feed has gone quiet, and a range that is never built is a
        session that never trades, which should not pass in silence.
        """
        # A session whose trading window has already closed is not waiting for
        # anything — that is just the previous day's session sitting in the
        # past at start-up, and warning about it is pure noise.
        if self.stop_enabled and now >= self.trade_until:
            return
        late = (now - self.session_end).total_seconds()
        if late > max(120.0, 2 * self.tf_seconds):
            self.log.warn_once(
                "rangewait",
                f"Range for {fmt_dt(self.session_start)}.."
                f"{fmt_time(self.session_end)} is still waiting for the "
                f"window's last {self.cfg.signal_timeframe} bar, "
                f"{late/60:.0f} min after the window closed. No bars are "
                f"arriving — the session cannot trade until they do.")

    def _is_late_session(self) -> bool:
        """Did this session's range window close before the EA started?

        `started_at` is set by the live path only, so this is always False in a
        backtest and nothing below can change a backtested trade.

        The test is against `session_end` — the moment range building finishes
        — not `session_start`. An EA that was already running when the window
        closed saw the first breakout itself and is properly in sync; one that
        started afterwards did not.
        """
        return (self.started_at is not None
                and self.session_end is not None
                and self.session_end <= self.started_at)

    def _adopt_late_session(self) -> None:
        """Join a session already under way, without repeating or ignoring it.

        Warm-up rebuilds the range from history but deliberately judges none of
        it, so the EA never sees this session's breakouts. Two things therefore
        have to be recovered before it may trade:

        HOW MUCH OF THE ALLOWANCE IS SPENT. The session's own history is
        replayed through the identical engine — `run_backtest`, `SimBroker`,
        this very strategy class and settings — and the trades it produces are
        adopted as `trades_this_session`. Verified on 193 Asia sessions of 2026
        data: the replay reproduced the full backtest's count in 193 of 193.

        This matters because a cruder measure is badly wrong. Counting range
        excursions over-counts by roughly 10x — 17 per session against 1.76
        real trades — and would declare the allowance spent in 91% of sessions.
        The replay puts the true figure at 46%, so more than half of
        late-joined sessions still have room, and abandoning them all would
        throw away real trades for no reason.

        MT5 is consulted too and the LARGER of the two is used: the replay
        catches trades the EA would have taken but did not (it was off, or an
        order was rejected), MT5 catches anything the replay cannot know about,
        such as a manual trade on the same magic.

        WHETHER A BREAKOUT MAY BE TAKEN AT ALL. Even with allowance left, the
        EA must not inherit a breakout it never saw — that would enter far from
        the range, where the stop is much wider because it is anchored to the
        range. Observed live: a 21:48 breakout entered at 23:38 with 2.7x the
        intended stop. So the session starts DISARMED and waits for a close
        back INSIDE the range. Once price is inside, the EA has witnessed the
        market itself and the next breakout is genuinely its own.

        If neither source can be read the session is skipped, because an
        unknown allowance cannot be spent safely.
        """
        d = self.broker.digits
        who = f"[{(self.cfg.name or 'MAIN')} / magic {self.cfg.magic}]"
        self.armed = False
        self._await_reentry = True

        replayed = None
        provider = getattr(self, "session_replay", None)
        if callable(provider):
            replayed = provider(self.session_start, self.session_end, self.cfg)
        actual = self.broker.trades_opened_since(self.cfg.magic, self.session_start)

        if replayed is None and actual is None:
            self._session_blocked = True
            self.log.warn(
                f"{who} Late start, and this session's history could not be read, so "
                f"how much of the {self.cfg.max_trades_per_session}-trade "
                f"allowance is already spent is unknown. NO trades this "
                f"session. The next session starts clean.")
            return

        used = max(v for v in (replayed, actual) if v is not None)
        self.trades_this_session = int(used)
        cap = self.cfg.max_trades_per_session
        source = []
        if replayed is not None:
            source.append(f"{replayed} from replaying its history")
        if actual is not None:
            source.append(f"{actual} opened on the account under magic "
                          f"{self.cfg.magic}")

        if 0 < cap <= used:
            self._session_blocked = True
            self.log.warn(
                f"{who} Late start — NO trades this session. It had already "
                f"used all {cap} of its trades before the EA started at "
                f"{fmt_dt(self.started_at)} ({', '.join(source)}). The range "
                f"below is still built and journalled.")
            return

        left = "unlimited" if cap <= 0 else str(cap - used)
        self.log.warn(
            f"{who} Late start: this session's range window closed at "
            f"{fmt_time(self.session_end)}, before the EA started at "
            f"{fmt_dt(self.started_at)}, so its early breakouts happened "
            f"unseen ({', '.join(source)}). {left} trade(s) of "
            f"{cap or 'unlimited'} remain. Waiting for a close back INSIDE "
            f"{self.range_low:.{d}f}..{self.range_high:.{d}f} before taking "
            f"one — a breakout the EA has actually witnessed, rather than one "
            f"it would be entering hours late and far from the range.")

    def _range_window_has_a_hole(self) -> bool:
        """Does this range window fall inside data neither source covered?

        The warm-up downloads history, which the API only serves up to some
        minutes behind now; the live subscription starts when the EA does.
        Between those two instants is a gap that no bar ever fills.

        For a window that opens after the EA started this is irrelevant, and
        for one that closed long before it, the warm-up covers everything. It
        bites in exactly one case: the EA is started WHILE a range window is in
        progress. Then part of the window is downloaded, part is live, and the
        minutes in between are simply absent — so the high and low are computed
        from a window with a hole in it.

        Observed: starting at 03:07 with history to 02:57 built London's
        03:00-03:15 range from a single bar covering 03:07-03:14, a range of
        2.00 where the true one was far wider. Nothing warned, and the session
        traded it. `_is_late_session` does not catch this — the EA started
        BEFORE the window ended, so by that test it is not late.
        """
        if not self.coverage_gap:
            return False
        gap_from, gap_to = self.coverage_gap
        if gap_to <= self.session_start:
            return False                      # gap ended before the window
        if gap_from is not None and gap_from >= self.session_end:
            return False                      # gap started after the window
        return True

    def _compute_range(self) -> None:
        bars = self.store.window(self.session_start, self.session_end)
        self.range_computed = True

        if self._range_window_has_a_hole():
            gap_from, gap_to = self.coverage_gap
            missing = ("everything before the EA started"
                       if gap_from is None
                       else f"{fmt_time(gap_from)}..{fmt_time(gap_to)}")
            self.range_valid = False
            self._session_blocked = True
            self.log.warn(
                f"[{(self.cfg.name or 'MAIN')}] Range window "
                f"{fmt_dt(self.session_start)}..{fmt_time(self.session_end)} "
                f"overlaps data NO source covered ({missing}) — the EA was "
                f"started while the window was still open, and the downloaded "
                f"history stops minutes short of the live feed. A high and low "
                f"taken from {len(bars)} of the window's bars would not be the "
                f"real range, so this session is skipped. Start the EA before "
                f"{fmt_time(self.session_start)} to trade it.")
            return

        if not bars:
            self.range_valid = False
            self.log.warn(
                f"No bars inside range window {fmt_dt(self.session_start)} .. "
                f"{fmt_dt(self.session_end)} - session skipped.")
            return
        hi = max(b.high for b in bars)
        lo = min(b.low for b in bars)
        self.range_high = hi
        self.range_low = lo
        self.range_mid = (hi + lo) / 2.0
        self.range_valid = hi > lo
        self.armed = self.range_valid           # ready for the first breakout
        if self.range_valid and self._is_late_session():
            self._adopt_late_session()
        d = self.broker.digits
        self.log.info(
            "Range built {a}..{b} | High {hi} | Low {lo} | Mid {mid} | size {sz} "
            "| {n} bars".format(
                a=fmt_dt(self.session_start), b=fmt_time(self.session_end),
                hi=f"{hi:.{d}f}", lo=f"{lo:.{d}f}", mid=f"{self.range_mid:.{d}f}",
                sz=f"{hi - lo:.{d}f}", n=len(bars)))
        if self.on_range_built:
            self.on_range_built(self)

    on_range_built = None      # optional callback for charting / dashboards

    # ------------------------------------------------------------------
    # Trading  (MQL5: GetStopPrice / OpenTrade / TradingAllowedNow)
    # ------------------------------------------------------------------
    def _stop_price(self, is_buy: bool) -> float:
        if self.cfg.sl_mode == SL_FULL_RANGE:
            return self.range_low if is_buy else self.range_high
        return self.range_mid

    def _matches(self, bucket, now: datetime) -> bool:
        """A day matches a bucket if either the session's own date or the
        current date is listed — the same two-sided check the EA always used."""
        dates = bucket[1]
        return dates.contains(now) or (self.session_start is not None and
                                       dates.contains(self.session_start))

    def _news_decision(self, now: datetime):
        """Combine every category into one allow/deny.

        Rules, in order:
          1. Any category set to OFF that lists this day  -> blocked. OFF is
             FINAL: it wins even when an ON or ONLY category also lists the
             day, and no combination of other categories can re-enable it.
             An explicit "never trade this" outranks everything else.
          2. If any category is set to ONLY, the day must be listed by at least
             one of them, otherwise blocked.
          3. Otherwise the day is tradeable.

        Returns (allowed, reason) — reason is "" when nothing is configured.

        PLANNED (agreed, not yet built): a day-of-week filter will sit
        alongside this one. The agreed precedence is that OFF is final across
        *both* filters — if the weekday filter says OFF, or any news category
        says OFF, the day is not tradeable, whatever the other says. So the
        combined gate is: blocked if (weekday OFF) or (news OFF), otherwise
        the ONLY rules below apply. Nothing here needs restructuring for it;
        `_trading_allowed_now` is the single gate every signal passes through.
        """
        if not self.active_buckets:
            return True, ""

        blocking = [b[0] for b in self.off_buckets if self._matches(b, now)]
        if blocking:
            hit_only = [b[0] for b in self.only_buckets if self._matches(b, now)]
            reason = f"{', '.join(blocking)} is OFF"
            if hit_only:
                reason += (f" (also listed by {', '.join(hit_only)} which is "
                           f"ONLY - OFF wins)")
            return False, reason

        if self.only_buckets:
            hit = [b[0] for b in self.only_buckets if self._matches(b, now)]
            if not hit:
                return False, ("not listed by any ONLY category ("
                               + ", ".join(b[0] for b in self.only_buckets) + ")")
            return True, f"matches {', '.join(hit)} (ONLY) - trading enabled"

        hit_off_free = [b[0] for b in self.active_buckets
                        if b[2] == NEWS_ON and self._matches(b, now)]
        if hit_off_free:
            return True, f"matches {', '.join(hit_off_free)} (ON) - trading enabled"
        return True, ""

    def _trading_allowed_now(self, now: datetime) -> bool:
        allowed, why = self._news_decision(now)
        if not allowed:
            self.log.info_once("newsfilter",
                               f"Signal ignored: {fmt_date(now)} - {why}.")
            return False
        if self.broker.positions_count() > 0:
            self.log.debug("Signal ignored: a position from this EA is already open.")
            return False
        if self._session_blocked:
            self.log.info_once(
                "sessionblocked",
                "Signal ignored: this session had already used its trade "
                "allowance before the EA started.")
            return False
        if 0 < self.cfg.max_trades_per_session <= self.trades_this_session:
            self.log.info_once(
                "maxtrades",
                f"Signal ignored: session limit of "
                f"{self.cfg.max_trades_per_session} trade(s) already reached.")
            return False
        return True

    # MetaTrader's order comment is short — 31 characters is the practical
    # limit, and some brokers reject an over-long one outright rather than
    # trimming it.
    COMMENT_LIMIT = 31

    def _order_comment(self) -> str:
        """`RangeBreak asia #2` — base text, session name, trade number.

        With several sessions running, every position would otherwise read the
        same word in the terminal. The session tag is the part that identifies
        the trade, so it is kept whole and the base text is trimmed if
        something has to give.
        """
        n = self.trades_this_session + 1          # this trade, not the last one
        tag = f"{self.cfg.name or 'MAIN'} #{n}"
        base = str(self.cfg.comment or "").strip()
        if not base:
            return tag[:self.COMMENT_LIMIT]
        room = self.COMMENT_LIMIT - len(tag) - 1
        if room <= 0:                              # a long session name wins
            return tag[:self.COMMENT_LIMIT]
        return f"{base[:room]} {tag}"

    def _open_trade(self, is_buy: bool) -> None:
        b = self.broker
        d = b.digits
        min_dist = b.stops_level_price

        # TWO price spaces. The signal is computed on the data feed (CME GC);
        # the order fills on the broker's symbol (spot XAUUSD). They track each
        # other but quote tens of dollars apart, so the RANGE LEVELS only mean
        # something in feed space. What crosses between them is the DISTANCE.
        exec_price = b.price_for(is_buy)              # where the order will fill
        if exec_price <= 0.0:
            return
        signal_price = b.reference_price(is_buy)      # where the signal fired
        if signal_price <= 0.0:
            signal_price = exec_price
        translate = bool(getattr(b, "translate_levels", False))

        stop_level = b.normalize_price(self._stop_price(is_buy))   # feed space
        sl_distance = abs(signal_price - stop_level)
        # the level to send with the order: measured from the price we expect
        # to fill at, so it is already in the broker's space
        sl = b.normalize_price(
            (exec_price - sl_distance) if is_buy else (exec_price + sl_distance)
        ) if translate else stop_level
        price = exec_price
        if sl_distance <= 0.0:
            self.log.warn("Signal skipped: entry price equals the Stop Loss level "
                          "(zero risk).")
            return
        if min_dist > 0.0 and sl_distance < min_dist:
            self.log.warn(
                f"Signal skipped: SL distance {sl_distance:.{d}f} is inside the "
                f"broker stop level {min_dist:.{d}f}.")
            return

        lot = b.normalize_lot(self.cfg.lots)
        if lot <= 0.0:
            self.log.error("Signal skipped: lot size resolves to zero - check symbol "
                           "volume limits.")
            return

        # send with the SL already attached (protection from tick one);
        # the TP is applied below, from the true execution price.
        ok, pos, err = b.open_market(is_buy, lot, sl, self._order_comment(),
                                     magic=self.cfg.magic)
        if not ok:
            # A dry run declining to send is the configuration working, not a
            # failure — logging it at ERROR made a correct `dry_run: true`
            # session read like a broken one. Everything else IS an error.
            # Behaviour is unchanged either way: the order did not open, so the
            # breakout stays armed and the session counter does not move.
            side = "BUY" if is_buy else "SELL"
            if str(err).strip().lower() == "dry run":
                self.log.info(f"{side} NOT sent — dry run. Still armed, so the "
                              f"next qualifying close will log another.")
            else:
                self.log.error(f"{side} order rejected: {err}")
            return

        self.trades_this_session += 1
        self.armed = False              # this breakout is spent; must re-qualify

        if pos is None:
            self.log.error("Position opened but could not be selected - TP was NOT "
                           "set. Check it manually.")
            return

        pos.session_start = self.session_start
        pos.session_name = self.cfg.name or "MAIN"
        pos.range_high, pos.range_low, pos.range_mid = \
            self.range_high, self.range_low, self.range_mid
        pos.trade_no_in_session = self.trades_this_session

        # Re-anchor on the ACTUAL fill. The order filled somewhere the broker
        # decided — slippage, a moving market, a different instrument — so the
        # levels are rebuilt from that price, keeping the exact distances the
        # signal asked for. Risk stays what the strategy intended no matter
        # where the fill landed.
        entry = pos.entry_price
        if translate:
            real_risk = sl_distance
            real_sl = b.normalize_price(
                (entry - real_risk) if is_buy else (entry + real_risk))
        else:
            real_sl = b.normalize_price(self._stop_price(is_buy))
            real_risk = abs(entry - real_sl)
        real_tp = b.normalize_price(entry + self.cfg.risk_reward * real_risk) if is_buy \
            else b.normalize_price(entry - self.cfg.risk_reward * real_risk)
        if translate:
            basis = getattr(b, "basis", None)
            self.log.info(
                "Levels carried across instruments | signal {sp} -> fill {e} "
                "(basis {bs}) | SL distance {rk} | TP distance {rw}".format(
                    sp=f"{signal_price:.{d}f}", e=f"{entry:.{d}f}",
                    bs=f"{(basis(is_buy) if callable(basis) else exec_price - signal_price):+.{d}f}",
                    rk=f"{real_risk:.{d}f}",
                    rw=f"{self.cfg.risk_reward * real_risk:.{d}f}"))

        if min_dist > 0.0 and abs(real_tp - entry) < min_dist:
            self.log.warn(
                f"TP NOT set on #{pos.ticket}: target {abs(real_tp - entry):.{d}f} "
                f"from entry is inside the broker stop level {min_dist:.{d}f}. "
                f"Position runs with SL only.")
            self._log_fill(is_buy, pos, lot, entry, real_sl, real_risk, 0.0)
            return

        ok, err = b.modify(pos, real_sl, real_tp)
        if not ok:
            self.log.error(f"Failed to set TP on #{pos.ticket}: {err}")
        else:
            self.log.debug(f"TP applied to #{pos.ticket}.")

        self._log_fill(is_buy, pos, lot, entry, real_sl, real_risk, real_tp)

    def _log_fill(self, is_buy, pos, lot, entry, sl, risk, tp) -> None:
        d = self.broker.digits
        self.log.info(
            "{dir} FILLED #{tk} | {lot:.2f} lots @ {e} | SL {sl} [{mode}] risk {rk} "
            "| TP {tp} reward {rw} | R:R 1:{rr:.2f} | trade {n} of session".format(
                dir=("BUY " if is_buy else "SELL"), tk=pos.ticket, lot=lot,
                e=f"{entry:.{d}f}", sl=f"{sl:.{d}f}",
                # same overridable label as the startup banner — a fill line
                # reading "[mid range]" on a stop that is 0.75 x the range is
                # the one place a live operator would notice a wrong config,
                # so it must describe what this engine actually did
                mode=self._stop_loss_label(),
                rk=f"{risk:.{d}f}", tp=f"{tp:.{d}f}",
                rw=f"{abs(tp - entry):.{d}f}" if tp else f"{0.0:.{d}f}",
                rr=self.cfg.risk_reward, n=self.trades_this_session))

    # ==================================================================
    # Main loop  (MQL5: OnTick)
    # ==================================================================
    def on_time(self, now: datetime) -> None:
        """Steps 1-3 of OnTick: session sync, range build, stop-time handling.

        Call this on every tick / every base bar, before feeding closed bars.
        """
        self.log.clock_time = now
        self._sync_session(now)

        # 1. build the range once the window has finished AND its bars are in
        if not self.range_computed and now >= self.session_end:
            if self._window_bars_are_in():
                self._compute_range()
            else:
                self._warn_waiting_for_window(now)

        # 2. stop time reached -> optionally flatten, then wait for next session
        if self.stop_enabled and now >= self.trade_until:
            if self.range_computed:
                self.log.info_once("windowclosed",
                                   "Stop Time reached - no further entries this session.")
            if self.cfg.close_at_stop_time and not self.closed_at_stop:
                self.closed_at_stop = True
                if self.broker.positions_count() > 0:
                    # The EA can only act when the market ticks. If the stop
                    # time falls inside a market break — the CME 17:00-18:00
                    # maintenance halt, or a whole weekend — the close happens
                    # on the first tick AFTER the break, not at the stop time.
                    # That is real behaviour, not a modelling shortcut, so say
                    # so loudly rather than let it hide in the equity curve.
                    late = (now - self.trade_until).total_seconds()
                    if late > self.tf_seconds:
                        self.log.warn(
                            f"Stop-time close is LATE by {late/3600:.1f}h: stop "
                            f"was {fmt_dt(self.trade_until)}, first tradeable "
                            f"price is {fmt_dt(now)}. The position was held "
                            f"through a closed market. Set a stop time that "
                            f"falls while the market is still trading.")
                    self.broker.close_all("stop time")
            return

    def in_trading_window(self, now: datetime) -> bool:
        if not self.range_computed or not self.range_valid:
            return False
        if now < self.session_end or now >= self.trade_until:
            return False
        return True

    def ingest_bar(self, bar: Bar) -> None:
        """Put a completed timeframe bar into history.

        Must be called *before* `on_time()` for the same moment, because in
        MT5 a bar is already in the history buffer on the tick that opens the
        next one — which is exactly the tick on which ComputeRange() runs.
        """
        self.store.add(bar)
        # Recorded HERE, where a bar actually enters history, and deliberately
        # not reused from `last_bar_time` — that one is set in `on_bar_closed`,
        # which runs AFTER `on_time` and returns early outside the trading
        # window. Gating the range build on it deadlocks: no range, so
        # `on_bar_closed` bails, so nothing is recorded, so no range. This
        # field has exactly one job and no early exits.
        self.last_ingested_time = bar.time

    def on_bar_closed(self, bar: Bar, now: datetime) -> None:
        """Steps 4-7 of OnTick, driven by a freshly closed signal-timeframe bar."""
        # step 2 guard: nothing may trade once the window is over
        if self.stop_enabled and now >= self.trade_until:
            return
        if not self.in_trading_window(now):
            return

        # 4. act only on a freshly closed bar
        if self.last_bar_time is not None and bar.time == self.last_bar_time:
            return
        self.last_bar_time = bar.time

        closed_open = bar.time
        closed_close = bar.close
        d = self.broker.digits

        # the bar must have closed after the range window ended
        if closed_open + timedelta(seconds=self.tf_seconds) <= self.session_end:
            return

        # 5. one position at a time
        if self.broker.positions_count() > 0:
            self.armed = False        # must re-qualify once this trade is finished
            self.log.debug("Position open - breakout already being traded, no new entry.")
            return

        # 6. re-arming
        inside_range = self.range_low <= closed_close <= self.range_high
        if not self.armed:
            # `_await_reentry` is the LATE-START gate and is deliberately
            # independent of `require_range_reentry`. That flag governs
            # re-arming after a trade closes; this one governs what the EA may
            # assume about breakouts it never saw. A config with re-entry
            # switched off must still not inherit a stale breakout.
            needs_inside = self.cfg.require_range_reentry or self._await_reentry
            if (not needs_inside) or inside_range:
                self.armed = True
                if self._await_reentry:
                    self._await_reentry = False
                    self.log.info(
                        f"In sync: bar {fmt_dt(closed_open)} closed at "
                        f"{closed_close:.{d}f}, back inside the range. The EA "
                        f"has now witnessed the market itself, so the next "
                        f"breakout can be traded.")
                else:
                    self.log.info(
                        f"Re-armed: bar {fmt_dt(closed_open)} closed at "
                        f"{closed_close:.{d}f} back inside the range - next breakout "
                        f"can trade.")
            else:
                self.log.debug(
                    f"Not armed: bar {fmt_dt(closed_open)} closed at "
                    f"{closed_close:.{d}f}, still outside the range.")
            return

        # 7. breakout signals
        buy_signal = closed_close > self.range_high
        sell_signal = closed_close < self.range_low
        if not buy_signal and not sell_signal:
            self.log.debug(f"Bar {fmt_dt(closed_open)} closed at {closed_close:.{d}f} "
                           f"- inside range, no signal.")
            return

        self.log.info(
            "BREAKOUT {dirn} | bar {t} closed at {c} vs Range {side} {lvl}".format(
                dirn=("UP" if buy_signal else "DOWN"),
                t=fmt_dt(closed_open), c=f"{closed_close:.{d}f}",
                side=("High" if buy_signal else "Low"),
                lvl=f"{(self.range_high if buy_signal else self.range_low):.{d}f}"))

        if not self._trading_allowed_now(now):
            return

        self._open_trade(buy_signal)

    # ------------------------------------------------------------------
    def report_exit(self, ticket: int, how: str, price: float,
                    net: float, currency: str) -> None:
        """MQL5 OnTradeTransaction() journal line."""
        d = self.broker.digits
        self.log.info(f"EXIT #{ticket} | {how} @ {price:.{d}f} | net {net:.2f} {currency}")
