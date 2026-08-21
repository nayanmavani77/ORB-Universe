"""Sweep axes for the `orb` engine.

The same grid `tools/run_matrix.py` has always swept, expressed once so a sweep
tool can ask the engine what its axes are instead of hard-coding them.

Session windows come from `orb.markets`, not from a table copied into this file.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Sequence

from ...config import AppConfig
from ...markets import SESSION_ORDER, range_window
from ..base import GridItem

TIMEFRAMES = ["M1", "M5", "M15"]
ORB_MINUTES = [15, 30, 60]
RISK_REWARD = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0]
SL_MODES = ["mid_range", "full_range"]
# How many trades one session may take. 1 = R, 2 = RR, 3 = RRR, 0 = unlimited.
# Same values and same spelling the `orb_reverse` grid already uses, so one
# `--set trade_caps=...` means the same thing whichever engine is being swept.
#
# It matters more than it looks: the cap is what decides turnover, and turnover
# is what decides whether an instrument survives its own dealing costs. A
# config that re-enters all session earns its result in many small pieces, each
# of which pays the spread.
TRADE_CAPS = [1, 2, 3, 0]
# Break-even trigger, as a multiple of the trade's own risk. `None` = the
# feature off entirely, which has to be in the list: the whole question is
# whether moving the stop to the entry helps at all, and that needs the same
# grid run both ways to answer.
BREAKEVENS = [None, 1.0, 1.5]
# (label used in run names, the news_trading value it means)
NEWS_MODES = [("INCLUDE_NEWS", "on"), ("SKIP_NEWS", "off")]

AXES: Dict[str, Sequence] = {
    "session": SESSION_ORDER,
    "signal_timeframe": TIMEFRAMES,
    "orb_minutes": ORB_MINUTES,
    "risk_reward": RISK_REWARD,
    "sl_mode": SL_MODES,
    "max_trades_per_session": TRADE_CAPS,
    "breakeven": BREAKEVENS,
    "news_mode": [label for label, _ in NEWS_MODES],
}


def _tag(value: float) -> str:
    return f"{value:g}".replace(".", "p")


def _breakeven(value):
    """One break-even axis value -> a trigger in R, or None for "off".

    `--set` hands every value through as text, so "off" arrives as the string
    `"off"`, not as YAML's `null`. Spelling it out here means the axis reads
    the same from the config file and from the command line, and a typo raises
    instead of silently sweeping a phantom setting.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("off", "none", "null", "no", "false", "0", ""):
        return None
    try:
        r = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"breakevens takes a trigger in R or 'off' (got {value!r}). "
            f"For example: --set breakevens=off,1,1.5") from None
    if r <= 0:
        raise ValueError(
            f"a break-even trigger must be greater than zero (got {r:g}). "
            f"Use 'off' to disable the feature.")
    return r


def build(base: AppConfig,
          sessions: Sequence[str] = tuple(SESSION_ORDER),
          timeframes: Sequence[str] = tuple(TIMEFRAMES),
          orb_minutes: Sequence[int] = tuple(ORB_MINUTES),
          risk_reward: Sequence[float] = tuple(RISK_REWARD),
          news_modes: Sequence[str] = ("INCLUDE_NEWS", "SKIP_NEWS"),
          sl_modes: Sequence[str] = ("mid_range",),
          trade_caps: Sequence[int] = (0,),
          breakevens: Sequence = (None,)) -> List[GridItem]:
    """Every combination requested, each as a ready-to-run `AppConfig`."""
    news_lookup = dict(NEWS_MODES)
    breakevens = [_breakeven(v) for v in breakevens]
    out: List[GridItem] = []
    for timeframe in timeframes:
        for news_label in news_modes:
            news_value = news_lookup[news_label]
            for session in sessions:
                for orb in orb_minutes:
                    for risk in risk_reward:
                        for sl_mode in sl_modes:
                          for cap in trade_caps:
                           for be in breakevens:
                            start, end, stop = range_window(session, orb)
                            run_name = (f"{timeframe}_{session.upper()}_ORB{orb}"
                                        f"_{news_label}_RR{_tag(risk)}")
                            if len(sl_modes) > 1:
                                run_name += f"_{sl_mode.upper()}"
                            if len(trade_caps) > 1:
                                # R / RR / RRR / ALL — the same shorthand the
                                # reversal grid uses in its run names
                                run_name += "_" + {0: "ALL", 1: "R", 2: "RR",
                                                   3: "RRR"}.get(int(cap),
                                                                 f"N{int(cap)}")
                            if len(breakevens) > 1:
                                # BE1 / BE1p5 for a trigger, NOBE for off —
                                # spelled out so a run name can never be read
                                # as "break-even was on at some default"
                                run_name += ("_NOBE" if be is None
                                             else f"_BE{_tag(float(be))}")
                            cfg = copy.deepcopy(base)
                            s = cfg.use_single_session(run_name)
                            s.engine = "orb"
                            s.engine_options = {}
                            s.signal_timeframe = timeframe
                            s.range_start, s.range_end, s.stop_time = start, end, stop
                            s.risk_reward = float(risk)
                            s.sl_mode = sl_mode
                            s.max_trades_per_session = int(cap)
                            s.breakeven = be is not None
                            if be is not None:
                                s.breakeven_trigger_r = float(be)
                            for _key, _label, cat in s.news.items():
                                cat.mode = news_value
                            s.news_days = ""
                            s.news_trading = news_value
                            s.log_level = "none"
                            cfg.server_timezone = "America/New_York"
                            cfg.server_utc_offset_hours = 0
                            out.append(GridItem(
                                run_name=run_name, cfg=cfg, engine="orb",
                                session=session.upper(),
                                signal_timeframe=timeframe, orb_minutes=orb,
                                news_mode=news_label, risk_reward=float(risk),
                                range_start=start, range_end=end,
                                stop_time=stop,
                                axes={"sl_mode": sl_mode,
                                      "max_trades_per_session": int(cap),
                                      "breakeven": ("off" if be is None
                                                    else f"{float(be):g}R")}))
    return out
