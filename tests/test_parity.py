"""Parity checks against the MQL5 rules, driven by synthetic bars.

Run:  python -m tests.test_parity      (from the project root)
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from orb.backtest import run_backtest                      # noqa: E402
from orb.bars import Bar, Resampler, bucket_start          # noqa: E402
from orb.config import AppConfig                           # noqa: E402
from orb.logger import RbeaLogger                          # noqa: E402
from orb.timeutils import (SkipDates, parse_date, parse_hhmm,  # noqa: E402
                           timeframe_seconds)

PASS = FAIL = 0


def check(name, got, want, tol=1e-9):
    global PASS, FAIL
    ok = (abs(got - want) <= tol) if isinstance(want, float) and \
        isinstance(got, (int, float)) else (got == want)
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, want {want!r}")


# --------------------------------------------------------------------------
def base_cfg(**strategy_over):
    raw = {
        "server_utc_offset_hours": 0,
        "strategy": {
            "range_start": "09:00", "range_end": "10:00", "stop_time": "17:00",
            "signal_timeframe": "M5", "sl_mode": "mid_range", "risk_reward": 2.0,
            "lots": 1.0, "require_range_reentry": True,
            "max_trades_per_session": 0, "close_at_stop_time": True,
            "log_level": "none",
        },
        "symbol": {
            "name": "TEST", "digits": 2, "point": 0.01, "tick_size": 0.01,
            "stops_level_points": 0, "volume_min": 1.0, "volume_max": 100.0,
            "volume_step": 1.0, "value_per_price_unit": 1.0, "currency": "USD",
        },
        "backtest": {"initial_balance": 10000.0, "dbn_paths": []},
    }
    raw["strategy"].update(strategy_over)
    return AppConfig.from_dict(raw)


def make_day(day: datetime, prices, start_hour=8, end_hour=18, wob=0.1):
    """One trading day of 1-minute bars.

    `prices` maps "HH:MM" -> price; the price holds until the next entry.
    Each bar is [p-wob, p+wob] with open == close == p.
    """
    keys = sorted(prices.items(), key=lambda kv: kv[0])
    bars = []
    cur = keys[0][1]
    ki = 0
    t = day.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    end = day.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    while t < end:
        stamp = t.strftime("%H:%M")
        while ki < len(keys) and keys[ki][0] <= stamp:
            cur = keys[ki][1]
            ki += 1
        bars.append(Bar(t, cur, cur + wob, cur - wob, cur, 1.0))
        t += timedelta(minutes=1)
    return bars


# ==========================================================================
print("\n--- helpers -------------------------------------------------------")
check("parse_hhmm 09:00", parse_hhmm("09:00")[0], 9 * 3600)
check("parse_hhmm 0 disabled", parse_hhmm("0")[1], True)
check("parse_hhmm 00:00 disabled", parse_hhmm("00:00")[1], True)
check("parse_date", parse_date("2026.04.03"), datetime(2026, 4, 3).date())
sd = SkipDates("2026.01.01,2026.04.06-2026.04.03")
check("skip single", sd.contains(datetime(2026, 1, 1, 12)), True)
check("skip reversed range start", sd.contains(datetime(2026, 4, 3, 9)), True)
check("skip reversed range end", sd.contains(datetime(2026, 4, 6, 23)), True)
check("skip outside", sd.contains(datetime(2026, 4, 7)), False)

# a long News Days list can be written in whatever shape is convenient
_warns = []
for _label, _src in [
        ("commas", "2026.01.13, 2026.02.11, 2026.03.11"),
        ("trailing comma", "2026.01.13, 2026.02.11, 2026.03.11,"),
        ("double comma", "2026.01.13,,2026.02.11,2026.03.11"),
        ("one per line", "2026.01.13\n2026.02.11\n2026.03.11"),
        ("lines and commas", "2026.01.13,\n2026.02.11,\n2026.03.11\n"),
        ("semicolons", " 2026.01.13 ; 2026.02.11 ; 2026.03.11 ")]:
    _n = SkipDates(_src, warn=lambda m: _warns.append(m))
    check(f"news list ({_label})", len(_n), 3)
    check(f"news list ({_label}) matches", _n.contains(datetime(2026, 2, 11)), True)
check("no spurious warnings from formatting", _warns, [])
_bad = []
SkipDates("2026.13.99, notadate", warn=lambda m: _bad.append(m))
check("real typos still warn", len(_bad), 2)
check("tf seconds M5", timeframe_seconds("M5"), 300)
check("bucket M5", bucket_start(datetime(2025, 1, 6, 10, 7), 300),
      datetime(2025, 1, 6, 10, 5))
check("bucket H4", bucket_start(datetime(2025, 1, 6, 10, 7), 14400),
      datetime(2025, 1, 6, 8, 0))

r = Resampler(300)
out = [r.push(Bar(datetime(2025, 1, 6, 10, m), 1, 2, 0, 1.5)) for m in range(0, 11)]
closed = [b for b in out if b is not None]
check("resampler emits on new bucket", len(closed), 2)
check("resampler bar open time", closed[0].time, datetime(2025, 1, 6, 10, 0))


# ==========================================================================
print("\n--- 1. range build + long breakout + TP ---------------------------")
day = datetime(2025, 1, 6)
bars = make_day(day, {
    "08:00": 100.5,
    "09:00": 100.5,          # range window: high 100.6, low 100.4, mid 100.5
    "10:00": 100.5,          # M5 bar 10:00-10:05 closes inside -> no signal
    "10:05": 101.0,          # M5 bar 10:05-10:10 closes 101.0 > 100.6 -> BUY
    "10:15": 101.5,          # walks up
    "10:20": 101.95,         # bar high 102.05 touches TP 102.00 -> filled at TP
})
cfg = base_cfg()
res = run_backtest(cfg, bars, RbeaLogger(level=0))
check("one trade taken", len(res.trades), 1)
t = res.trades[0]
check("direction", t.direction, "BUY")
check("range high", t.range_high, 100.6)
check("range low", t.range_low, 100.4)
check("range mid", t.range_mid, 100.5)
check("entry at 10:10 open", t.entry_time, datetime(2025, 1, 6, 10, 10))
check("entry price", t.entry_price, 101.0)
check("SL = mid range", t.sl, 100.5)
check("TP = entry + 2 x risk", t.tp, 102.0)   # risk 0.5 -> TP 102.00
check("exit reason", t.exit_reason, "TAKE PROFIT hit")
check("net profit", t.net_profit, 1.0)        # (102-101) x 1 lot x 1.0/point


# ==========================================================================
print("\n--- 2. full-range stop loss ---------------------------------------")
cfg = base_cfg(sl_mode="full_range")
res = run_backtest(cfg, bars, RbeaLogger(level=0))
t = res.trades[0]
check("SL = range low", t.sl, 100.4)
check("TP = entry + 2 x (entry - low)", t.tp, 102.2)


# ==========================================================================
print("\n--- 3. short breakout ---------------------------------------------")
bars_s = make_day(day, {
    "08:00": 100.5, "09:00": 100.5,
    "10:05": 100.0,          # closes 100.0 < 100.4 -> SELL
    "10:15": 99.5,
    "10:20": 99.05,          # bar low 98.95 touches TP 99.00 -> filled at TP
})
cfg = base_cfg()
res = run_backtest(cfg, bars_s, RbeaLogger(level=0))
t = res.trades[0]
check("direction", t.direction, "SELL")
check("entry price", t.entry_price, 100.0)
check("SL = mid range", t.sl, 100.5)
check("TP = entry - 2 x risk", t.tp, 99.0)
check("exit reason", t.exit_reason, "TAKE PROFIT hit")


# ==========================================================================
print("\n--- 4. stop loss hit ----------------------------------------------")
bars_sl = make_day(day, {
    "08:00": 100.5, "09:00": 100.5,
    "10:05": 101.0,          # BUY at 10:10 @ 101.0, SL 100.5
    "10:20": 100.55,         # bar low 100.45 touches SL 100.50 -> filled at SL
    "10:40": 100.45,         # closes back inside the range -> re-arm
    "11:00": 100.45,
})
cfg = base_cfg()
res = run_backtest(cfg, bars_sl, RbeaLogger(level=0))
check("trades", len(res.trades), 1)
check("exit reason", res.trades[0].exit_reason, "STOP LOSS hit")
check("net profit", res.trades[0].net_profit, -0.5)

# a bar that gaps straight through the level fills at the bar OPEN, not the level
bars_gap = make_day(day, {
    "08:00": 100.5, "09:00": 100.5,
    "10:05": 101.0,          # BUY @ 101.0, SL 100.5
    "10:20": 100.2,          # opens below the stop -> gap fill at 100.20
    "10:40": 100.45, "11:00": 100.45,
})
res = run_backtest(base_cfg(), bars_gap, RbeaLogger(level=0))
check("gap through SL fills at bar open", res.trades[0].exit_price, 100.2)


# ==========================================================================
print("\n--- 5. re-entry rule ----------------------------------------------")
# after the stop, price stays OUTSIDE the range -> with re-entry required
# no second trade may be taken, without it a second trade is taken.
bars_re = make_day(day, {
    "08:00": 100.5, "09:00": 100.5,
    "10:05": 101.0,          # BUY -> stopped out below
    "10:20": 100.2,
    "10:25": 101.5,          # still outside (above) the range
    "11:00": 101.5,
})
res = run_backtest(base_cfg(), bars_re, RbeaLogger(level=0))
check("re-entry required -> 1 trade", len(res.trades), 1)
res = run_backtest(base_cfg(require_range_reentry=False), bars_re, RbeaLogger(level=0))
check("re-entry not required -> 2 trades", len(res.trades), 2)

# with a close back inside the range the EA re-arms and takes the next breakout
bars_re2 = make_day(day, {
    "08:00": 100.5, "09:00": 100.5,
    "10:05": 101.0,          # BUY -> stopped out
    "10:20": 100.2,
    "10:30": 100.45,         # back INSIDE the range -> re-armed
    "10:40": 101.2,          # new breakout -> second trade
    "11:00": 101.2,
})
res = run_backtest(base_cfg(), bars_re2, RbeaLogger(level=0))
check("re-armed after inside close -> 2 trades", len(res.trades), 2)


# ==========================================================================
print("\n--- 6. max trades per session --------------------------------------")
res = run_backtest(base_cfg(max_trades_per_session=1), bars_re2, RbeaLogger(level=0))
check("session capped at 1", len(res.trades), 1)


# ==========================================================================
print("\n--- 7. News Days: the three modes -----------------------------------")
# `bars` is a single session on 2025-01-06 that produces exactly one trade.
NEWS = "2025.01.06"

# OFF: a News Day is not traded
res = run_backtest(base_cfg(news_days=NEWS, news_trading="off"), bars,
                   RbeaLogger(level=0))
check("OFF + news day  -> no trades", len(res.trades), 0)

# OFF: a day that is NOT a News Day trades normally
res = run_backtest(base_cfg(news_days="2030.01.01", news_trading="off"), bars,
                   RbeaLogger(level=0))
check("OFF + normal day -> trades", len(res.trades), 1)

# ON: everything trades, the list is ignored
res = run_backtest(base_cfg(news_days=NEWS, news_trading="on"), bars,
                   RbeaLogger(level=0))
check("ON  + news day  -> trades", len(res.trades), 1)
res = run_backtest(base_cfg(news_days="2030.01.01", news_trading="on"), bars,
                   RbeaLogger(level=0))
check("ON  + normal day -> trades", len(res.trades), 1)

# ONLY: News Days trade, everything else does not
res = run_backtest(base_cfg(news_days=NEWS, news_trading="only"), bars,
                   RbeaLogger(level=0))
check("ONLY + news day  -> trades", len(res.trades), 1)
res = run_backtest(base_cfg(news_days="2030.01.01", news_trading="only"), bars,
                   RbeaLogger(level=0))
check("ONLY + normal day -> no trades", len(res.trades), 0)

# the modes must partition the days: ON == OFF + ONLY, with no overlap
multi_news = []
for k in range(5):
    dd = datetime(2025, 3, 3) + timedelta(days=k)          # Mon-Fri
    multi_news += make_day(dd, {"08:00": 100.5, "09:00": 100.5,
                                "10:05": 101.0, "10:15": 101.5, "10:20": 101.95})
NEWS2 = "2025.03.04,2025.03.06"                            # Tue and Thu
n_on = len(run_backtest(base_cfg(news_days=NEWS2, news_trading="on"),
                        multi_news, RbeaLogger(level=0)).trades)
r_off = run_backtest(base_cfg(news_days=NEWS2, news_trading="off"),
                     multi_news, RbeaLogger(level=0)).trades
r_only = run_backtest(base_cfg(news_days=NEWS2, news_trading="only"),
                      multi_news, RbeaLogger(level=0)).trades
check("ON trades every day", n_on, 5)
check("OFF skips the 2 news days", len(r_off), 3)
check("ONLY takes only the 2 news days", len(r_only), 2)
check("OFF + ONLY == ON", len(r_off) + len(r_only), n_on)
off_days = {t.entry_time.date() for t in r_off}
only_days = {t.entry_time.date() for t in r_only}
check("OFF and ONLY never overlap", off_days & only_days, set())
check("ONLY hit the listed dates",
      sorted(str(d) for d in only_days), ["2025-03-04", "2025-03-06"])

# date ranges work in every mode
rng = run_backtest(base_cfg(news_days="2025.03.04-2025.03.06", news_trading="only"),
                   multi_news, RbeaLogger(level=0)).trades
check("ONLY with a date range", len(rng), 3)

# guard: "only" with an empty list would silently never trade
try:
    base_cfg(news_days="", news_trading="only")
    check("empty list + ONLY is rejected", "no error", "ValueError")
except ValueError as exc:
    check("empty list + ONLY is rejected", "never trade" in str(exc), True)

# guard: a bad mode name is rejected
try:
    base_cfg(news_trading="maybe")
    check("bad news_trading rejected", "no error", "ValueError")
except ValueError:
    check("bad news_trading rejected", True, True)


# ==========================================================================
print("\n--- 7b. News Days by category ---------------------------------------")
# five weekdays, one trade each
cat_bars = []
for k in range(5):                                          # Mon 3 - Fri 7 Mar
    dd = datetime(2025, 3, 3) + timedelta(days=k)
    cat_bars += make_day(dd, {"08:00": 100.5, "09:00": 100.5,
                              "10:05": 101.0, "10:15": 101.5, "10:20": 101.95})
# Five consecutive trading DATES. The names are just a readable shorthand for
# which day of that particular week each date falls on — the filter itself
# matches on the calendar date and has no day-of-week rule whatsoever. The
# "date, not weekday" test below pins that.
MON, TUE, WED, THU, FRI = ("2025.03.03", "2025.03.04", "2025.03.05",
                           "2025.03.06", "2025.03.07")


def news_cfg(**cats):
    raw = {
        "server_utc_offset_hours": 0,
        "strategy": {
            "range_start": "09:00", "range_end": "10:00", "stop_time": "17:00",
            "signal_timeframe": "M5", "sl_mode": "mid_range", "risk_reward": 2.0,
            "lots": 1.0, "log_level": "none",
            "news": {k: v for k, v in cats.items()},
        },
        "symbol": {"name": "TEST", "digits": 2, "point": 0.01, "tick_size": 0.01,
                   "stops_level_points": 0, "volume_min": 1.0, "volume_max": 100.0,
                   "volume_step": 1.0, "value_per_price_unit": 1.0,
                   "currency": "USD"},
        "backtest": {"initial_balance": 10000.0, "dbn_paths": []},
    }
    return AppConfig.from_dict(raw)


def days_traded(cfg):
    r = run_backtest(cfg, cat_bars, RbeaLogger(level=0))
    return sorted(t.entry_time.strftime("%Y.%m.%d") for t in r.trades)


check("no categories -> every day trades",
      days_traded(news_cfg()), [MON, TUE, WED, THU, FRI])

# one category OFF removes just its own dates
check("CPI off removes CPI days",
      days_traded(news_cfg(core_cpi_mm={"mode": "off", "dates": f"{TUE},{THU}"})),
      [MON, WED, FRI])

# a second category OFF removes its dates too — they accumulate
check("CPI off + NFP off remove both",
      days_traded(news_cfg(core_cpi_mm={"mode": "off", "dates": TUE},
                           non_farm_employment_change={"mode": "off", "dates": FRI})),
      [MON, WED, THU])

# ON is inert: its dates are not restricted, and it does not open anything
check("ON category is inert",
      days_traded(news_cfg(core_cpi_mm={"mode": "on", "dates": TUE},
                           core_ppi_mm={"mode": "off", "dates": WED})),
      [MON, TUE, THU, FRI])

# ONLY restricts to its own dates
check("FOMC only",
      days_traded(news_cfg(federal_funds_rate={"mode": "only", "dates": WED})),
      [WED])

# two ONLY categories are a union
check("two ONLY categories union",
      days_traded(news_cfg(federal_funds_rate={"mode": "only", "dates": WED},
                           ism_services_pmi={"mode": "only", "dates": FRI})),
      [WED, FRI])

# OFF vetoes ONLY on a day listed by both
check("OFF beats ONLY on the same day",
      days_traded(news_cfg(federal_funds_rate={"mode": "only", "dates": f"{WED},{THU}"},
                           core_cpi_mm={"mode": "off", "dates": WED})),
      [THU])

# categories are independent: same date, different category, different mode
check("independent categories",
      days_traded(news_cfg(core_cpi_mm={"mode": "off", "dates": MON},
                           unemployment_rate={"mode": "only", "dates": f"{MON},{TUE}"})),
      [TUE])

# a date range inside a category
check("category with a date range",
      days_traded(news_cfg(ism_manufacturing_pmi={"mode": "off",
                                                  "dates": f"{TUE}-{THU}"})),
      [MON, FRI])

# the general bucket still works alongside categories
raw_gen = news_cfg(core_cpi_mm={"mode": "off", "dates": TUE})
raw_gen.strategy.news_days = FRI
raw_gen.strategy.news_trading = "off"
check("general bucket works alongside categories",
      days_traded(raw_gen), [MON, WED, THU])

# a category with dates but no matching day changes nothing
check("non-matching category is harmless",
      days_traded(news_cfg(core_pce_price_index_mm={"mode": "off",
                                                    "dates": "2030.01.01"})),
      [MON, TUE, WED, THU, FRI])

# --- the filter is by DATE, never by day of the week -------------------
# WED is the DATE 2025.03.05, which happens to fall on a Wednesday. Listing
# it must block that one date only, and leave every other Wednesday alone.
_multi = []
for _k in range(15):
    _dd = datetime(2025, 3, 3) + timedelta(days=_k)
    if _dd.weekday() >= 5:
        continue
    _multi += make_day(_dd, {"08:00": 100.5, "09:00": 100.5,
                             "10:05": 101.0, "10:15": 101.5, "10:20": 101.95})
_r = run_backtest(news_cfg(core_cpi_mm={"mode": "off", "dates": WED}),
                  _multi, RbeaLogger(level=0))
_dates = {t.entry_time.strftime("%Y.%m.%d") for t in _r.trades}
check("the listed date is blocked", "2025.03.05" in _dates, False)
check("the NEXT Wednesday still trades", "2025.03.12" in _dates, True)
check("no other date is affected", len(_dates), 10)


# --- OFF IS FINAL -----------------------------------------------------
# A date in an OFF category is never traded, no matter what any other
# category says about that same date. This is the deciding rule when modes
# conflict, so every combination is pinned here.
_off_cases = [
    ("OFF alone",
     dict(core_cpi_mm={"mode": "off", "dates": WED})),
    ("OFF + the same date ON",
     dict(core_cpi_mm={"mode": "off", "dates": WED},
          non_farm_employment_change={"mode": "on", "dates": WED})),
    ("OFF + the same date ONLY",
     dict(core_cpi_mm={"mode": "off", "dates": WED},
          federal_funds_rate={"mode": "only", "dates": f"{WED},{THU}"})),
    ("OFF + ON + ONLY together",
     dict(core_cpi_mm={"mode": "off", "dates": WED},
          non_farm_employment_change={"mode": "on", "dates": WED},
          federal_funds_rate={"mode": "only", "dates": f"{WED},{THU}"})),
    ("OFF + two ONLY categories",
     dict(core_cpi_mm={"mode": "off", "dates": WED},
          federal_funds_rate={"mode": "only", "dates": f"{WED},{THU}"},
          ism_services_pmi={"mode": "only", "dates": f"{WED},{FRI}"})),
    ("two OFF categories both listing it",
     dict(core_cpi_mm={"mode": "off", "dates": WED},
          core_ppi_mm={"mode": "off", "dates": WED},
          federal_funds_rate={"mode": "only", "dates": f"{WED},{THU}"})),
]
for _name, _cats in _off_cases:
    check(f"OFF is final: {_name}", WED in days_traded(news_cfg(**_cats)), False)

# the general bucket is just another category, so OFF there is final too
_g = news_cfg(federal_funds_rate={"mode": "only", "dates": f"{WED},{THU}"})
_g.strategy.news_days = WED
_g.strategy.news_trading = "off"
check("OFF is final: general bucket OFF beats a category ONLY",
      days_traded(_g), [THU])

# and the reverse direction: a category OFF beats the general bucket ONLY
_g2 = news_cfg(core_cpi_mm={"mode": "off", "dates": WED})
_g2.strategy.news_days = f"{WED},{THU}"
_g2.strategy.news_trading = "only"
check("OFF is final: category OFF beats the general bucket ONLY",
      days_traded(_g2), [THU])

# guards
try:
    news_cfg(core_cpi_mm={"mode": "only", "dates": ""}).strategy.validate()
    check("category only+empty rejected", "no error", "ValueError")
except ValueError as exc:
    check("category only+empty rejected", "never trade" in str(exc), True)
try:
    news_cfg(core_cpi_mm={"mode": "sometimes", "dates": TUE}).strategy.validate()
    check("bad category mode rejected", "no error", "ValueError")
except ValueError:
    check("bad category mode rejected", True, True)
try:
    AppConfig.from_dict({"strategy": {"news": {"not_a_category": {"mode": "off"}}}})
    check("unknown category rejected", "no error", "ValueError")
except ValueError:
    check("unknown category rejected", True, True)


print("\n--- 8. close at stop time -------------------------------------------")
bars_st = make_day(day, {
    "08:00": 100.5, "09:00": 100.5,
    "10:05": 101.0,          # BUY, never reaches SL or TP
    "10:10": 101.2,
}, end_hour=18)
res = run_backtest(base_cfg(stop_time="17:00"), bars_st, RbeaLogger(level=0))
t = res.trades[0]
check("closed by stop time", t.exit_reason, "stop time")
check("closed at 17:00", t.exit_time, datetime(2025, 1, 6, 17, 0))

res = run_backtest(base_cfg(stop_time="17:00", close_at_stop_time=False),
                   bars_st, RbeaLogger(level=0))
check("stop time without flatten -> stays open to EOD",
      res.trades[0].exit_reason, "end of backtest")


# ==========================================================================
print("\n--- 9. bar must close AFTER the range window ------------------------")
# a breakout that happens INSIDE the range window must never trade
bars_in = make_day(day, {
    "08:00": 100.5, "09:00": 100.5,
    "09:30": 105.0,          # spike inside the window -> becomes part of the range
    "09:40": 100.5,
    "10:05": 100.5,          # after the window: inside the (now larger) range
    "11:00": 100.5,
})
res = run_backtest(base_cfg(), bars_in, RbeaLogger(level=0))
check("no trade from an in-window move", len(res.trades), 0)


# ==========================================================================
print("\n--- 10. multi-day session rollover ----------------------------------")
multi = []
for k in range(3):
    d = datetime(2025, 1, 6) + timedelta(days=k)
    multi += make_day(d, {
        "08:00": 100.5, "09:00": 100.5,
        "10:05": 101.0, "10:15": 101.5, "10:20": 101.95,
    })
res = run_backtest(base_cfg(), multi, RbeaLogger(level=0))
check("one trade per day", len(res.trades), 3)
check("all take profit", all(t.exit_reason == "TAKE PROFIT hit" for t in res.trades), True)
check("balance", res.final_balance, 10003.0)

sessions = {t.session_start for t in res.trades}
check("three distinct sessions", len(sessions), 3)


# ==========================================================================
print("\n--- 11. stop time = 0 (continuous) ----------------------------------")
bars_c = make_day(day, {"08:00": 100.5, "09:00": 100.5, "10:05": 101.0,
                        "10:10": 101.2}, end_hour=23)
res = run_backtest(base_cfg(stop_time="0"), bars_c, RbeaLogger(level=0))
check("no stop-time close", res.trades[0].exit_reason, "end of backtest")


# ==========================================================================
print("\n--- 11b. market closed: weekends and holidays ------------------------")
# Two full weeks of bars, Monday-Friday only. There is simply no data on
# Saturday or Sunday, so the range for those "sessions" cannot be built and
# no trade may ever be opened on them.
weeks = []
for k in range(14):
    dd = datetime(2025, 3, 3) + timedelta(days=k)     # Mon 3 Mar 2025
    if dd.weekday() >= 5:                             # market closed
        continue
    weeks += make_day(dd, {"08:00": 100.5, "09:00": 100.5,
                           "10:05": 101.0, "10:15": 101.5, "10:20": 101.95})
res = run_backtest(base_cfg(), weeks, RbeaLogger(level=0))
wdays = {t.entry_time.weekday() for t in res.trades}
check("trades only on weekdays", max(wdays) <= 4, True)
check("no Saturday trades", any(t.entry_time.weekday() == 5 for t in res.trades), False)
check("no Sunday trades", any(t.entry_time.weekday() == 6 for t in res.trades), False)
check("one trade per open day", len(res.trades), 10)

# a mid-week holiday (no bars at all that day) must be skipped cleanly and
# must not disturb the following session
holiday = []
for k in range(5):
    dd = datetime(2025, 3, 3) + timedelta(days=k)
    if k == 2:                                        # Wednesday closed
        continue
    holiday += make_day(dd, {"08:00": 100.5, "09:00": 100.5,
                             "10:05": 101.0, "10:15": 101.5, "10:20": 101.95})
res = run_backtest(base_cfg(), holiday, RbeaLogger(level=0))
check("holiday produces no trade", len(res.trades), 4)
check("day after the holiday still trades",
      any(t.entry_time.date() == datetime(2025, 3, 6).date() for t in res.trades), True)

# a day where the market opens AFTER the range window closes: the range cannot
# be built, so that session takes no trades
late = make_day(datetime(2025, 3, 10), {"11:00": 100.5, "12:00": 101.0},
                start_hour=11, end_hour=18)
res = run_backtest(base_cfg(), late, RbeaLogger(level=0))
check("no range, no trades", len(res.trades), 0)


print("\n--- 12. range window boundaries -------------------------------------")
# the LAST timeframe bar of the window (09:55-10:00) must be included, and the
# first bar AFTER the window (10:00) must not be.
bars_b = make_day(day, {
    "08:00": 100.5,
    "08:55": 120.0,          # before the window -> must be ignored
    "09:00": 100.5,
    "09:55": 100.8,          # last bar of the window -> sets the high
    "10:00": 130.0,          # after the window -> must be ignored
    "10:05": 100.5, "11:00": 100.5,
})
cfg = base_cfg()
from orb.bars import BarStore                                   # noqa: E402
from orb.broker import SimBroker                                # noqa: E402
from orb.strategy import RangeBreakoutStrategy                  # noqa: E402
_b = SimBroker(cfg.symbol, 10000.0)
_s = RangeBreakoutStrategy(cfg.strategy, _b, BarStore(), RbeaLogger(level=0))
_r = Resampler(300)
for _bar in bars_b:
    _b.set_market(_bar.open, _bar.time)
    _c = _r.push(_bar)
    if _c is not None:
        _s.ingest_bar(_c)
    _s.on_time(_bar.time)
    if _c is not None:
        _s.on_bar_closed(_c, _bar.time)
check("range high includes the 09:55 bar", _s.range_high, 100.9)
check("range low", _s.range_low, 100.4)
check("range excludes pre/post window bars", _s.range_high < 120.0, True)
check("range mid", _s.range_mid, 100.65)


print("\n--- 13. report generation -------------------------------------------")
from orb.report import compute_stats, breakdowns, trades_dataframe, build_html  # noqa: E402
res = run_backtest(base_cfg(), multi, RbeaLogger(level=0))
stats = compute_stats(res)
df = trades_dataframe(res.trades)
bd = breakdowns(df)
htm = build_html(res, stats, df, bd)
check("stats total trades", stats["total_trades"], 3)
check("stats win rate", stats["win_rate"], 100.0)
check("daily rows", len(bd["daily"]), 3)
check("hourly rows", len(bd["hourly"]), 1)
check("html built", len(htm) > 5000, True)


# ==========================================================================
print("\n" + "=" * 62)
print(f"  {PASS} passed, {FAIL} failed")
print("=" * 62)
sys.exit(1 if FAIL else 0)
