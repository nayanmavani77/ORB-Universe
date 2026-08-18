"""Sweep axes for the `orb_reverse` engine.

Session windows come from `orb.markets` — the shared table. This file used to
carry a verbatim copy of it, with a comment arguing the duplication was
deliberate. It was not worth it: the two copies had to be edited together, and a
market open is a fact about the market, not about an engine.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ...config import AppConfig
from ...markets import SESSION_ORDER, range_window
from ..base import GridItem
from .settings import FORWARD, REVERSE, OrbReverseSettings

TIMEFRAMES = ["M1", "M5", "M15"]
ORB_MINUTES = [15, 30, 60]
RISK_REWARD = [1.0, 1.5, 2.0, 2.5, 3.0]
# 0.5 and 1.0 are the `orb` engine's two stop modes; the rest is ground it
# cannot reach.
SL_RANGE_MULTS = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0]
# 1 = R, 2 = RR, 3 = RRR, 0 = unlimited
TRADE_CAPS = [1, 2, 3]
DIRECTIONS = [REVERSE, FORWARD]
# (label used in run names, the news_trading value it means)
NEWS_MODES = [("INCLUDE_NEWS", "on"), ("SKIP_NEWS", "off")]

AXES: Dict[str, Sequence] = {
    "session": SESSION_ORDER,
    "signal_timeframe": TIMEFRAMES,
    "orb_minutes": ORB_MINUTES,
    "risk_reward": RISK_REWARD,
    "sl_range_mult": SL_RANGE_MULTS,
    "max_trades_per_session": TRADE_CAPS,
    "direction": DIRECTIONS,
    "news_mode": [label for label, _ in NEWS_MODES],
}


def _tag(value: float) -> str:
    return f"{value:g}".replace(".", "p")


def build(base: AppConfig,
          sessions: Sequence[str] = ("LONDON",),
          timeframes: Sequence[str] = tuple(TIMEFRAMES),
          orb_minutes: Sequence[int] = tuple(ORB_MINUTES),
          news_modes: Sequence[str] = ("INCLUDE_NEWS", "SKIP_NEWS"),
          risk_reward: Sequence[float] = tuple(RISK_REWARD),
          sl_range_mults: Sequence[float] = tuple(SL_RANGE_MULTS),
          trade_caps: Sequence[int] = tuple(TRADE_CAPS),
          directions: Sequence[str] = (REVERSE,),
          sl_anchor: Optional[str] = None) -> List[GridItem]:
    """Every combination requested, each as a ready-to-run `AppConfig`.

    Including `forward` in `directions` gives the control arm: same stop
    multiplier, same cap, ordinary breakout direction.
    """
    news_lookup = dict(NEWS_MODES)
    out: List[GridItem] = []
    for timeframe in timeframes:
        for news_label in news_modes:
            news_value = news_lookup[news_label]
            for session in sessions:
                session = str(session).upper()
                for orb in orb_minutes:
                    for risk in risk_reward:
                        for mult in sl_range_mults:
                            for cap in trade_caps:
                                for direction in directions:
                                    settings = OrbReverseSettings(
                                        sl_range_mult=float(mult),
                                        direction=str(direction))
                                    if sl_anchor:
                                        settings.sl_anchor = sl_anchor
                                    settings.validate()
                                    start, end, stop = range_window(session, orb)
                                    run_name = (
                                        f"{timeframe}_{session}_ORB{orb}_"
                                        f"{news_label}_RR{_tag(risk)}_"
                                        f"SL{_tag(mult)}_"
                                        f"{settings.run_name(cap).split('_')[-1]}_"
                                        f"{'REV' if settings.reverse else 'FWD'}")
                                    cfg = copy.deepcopy(base)
                                    s = cfg.use_single_session(run_name)
                                    s.signal_timeframe = timeframe
                                    s.range_start = start
                                    s.range_end = end
                                    s.stop_time = stop
                                    s.risk_reward = float(risk)
                                    for _k, _l, cat in s.news.items():
                                        cat.mode = news_value
                                    s.news_days = ""
                                    s.news_trading = news_value
                                    s.log_level = "none"
                                    cfg.server_timezone = "America/New_York"
                                    cfg.server_utc_offset_hours = 0
                                    settings.apply_to_session(s)
                                    s.max_trades_per_session = int(cap)
                                    out.append(GridItem(
                                        run_name=run_name, cfg=cfg,
                                        settings=settings, engine="orb_reverse",
                                        session=session,
                                        signal_timeframe=timeframe,
                                        orb_minutes=orb, news_mode=news_label,
                                        risk_reward=float(risk),
                                        range_start=start, range_end=end,
                                        stop_time=stop,
                                        axes={
                                            "sl_range_mult": float(mult),
                                            "max_trades_per_session": int(cap),
                                            "direction": settings.direction,
                                        }))
    return out
