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
# (label used in run names, the news_trading value it means)
NEWS_MODES = [("INCLUDE_NEWS", "on"), ("SKIP_NEWS", "off")]

AXES: Dict[str, Sequence] = {
    "session": SESSION_ORDER,
    "signal_timeframe": TIMEFRAMES,
    "orb_minutes": ORB_MINUTES,
    "risk_reward": RISK_REWARD,
    "sl_mode": SL_MODES,
    "news_mode": [label for label, _ in NEWS_MODES],
}


def _tag(value: float) -> str:
    return f"{value:g}".replace(".", "p")


def build(base: AppConfig,
          sessions: Sequence[str] = tuple(SESSION_ORDER),
          timeframes: Sequence[str] = tuple(TIMEFRAMES),
          orb_minutes: Sequence[int] = tuple(ORB_MINUTES),
          risk_reward: Sequence[float] = tuple(RISK_REWARD),
          news_modes: Sequence[str] = ("INCLUDE_NEWS", "SKIP_NEWS"),
          sl_modes: Sequence[str] = ("mid_range",)) -> List[GridItem]:
    """Every combination requested, each as a ready-to-run `AppConfig`."""
    news_lookup = dict(NEWS_MODES)
    out: List[GridItem] = []
    for timeframe in timeframes:
        for news_label in news_modes:
            news_value = news_lookup[news_label]
            for session in sessions:
                for orb in orb_minutes:
                    for risk in risk_reward:
                        for sl_mode in sl_modes:
                            start, end, stop = range_window(session, orb)
                            run_name = (f"{timeframe}_{session.upper()}_ORB{orb}"
                                        f"_{news_label}_RR{_tag(risk)}")
                            if len(sl_modes) > 1:
                                run_name += f"_{sl_mode.upper()}"
                            cfg = copy.deepcopy(base)
                            s = cfg.use_single_session(run_name)
                            s.engine = "orb"
                            s.engine_options = {}
                            s.signal_timeframe = timeframe
                            s.range_start, s.range_end, s.stop_time = start, end, stop
                            s.risk_reward = float(risk)
                            s.sl_mode = sl_mode
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
                                axes={"sl_mode": sl_mode}))
    return out
