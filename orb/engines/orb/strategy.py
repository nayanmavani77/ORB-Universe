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

        self._banner()

    # ------------------------------------------------------------------
    # OnInit banner
    # ------------------------------------------------------------------
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
                sl=("full range" if self.cfg.sl_mode == SL_FULL_RANGE else "mid range"),
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
    def _compute_range(self) -> None:
        bars = self.store.window(self.session_start, self.session_end)
        self.range_computed = True
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
            self.log.error(f"{'BUY' if is_buy else 'SELL'} order rejected: {err}")
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
                mode=("full range" if self.cfg.sl_mode == SL_FULL_RANGE else "mid range"),
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

        # 1. build the range once the window has finished
        if not self.range_computed and now >= self.session_end:
            self._compute_range()

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
            if (not self.cfg.require_range_reentry) or inside_range:
                self.armed = True
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
