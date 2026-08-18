"""The trading day: when each session opens, and when it must be flat.

One definition, shared by every engine's `grid.py` and by `tools/run_matrix.py`.
It used to be copied verbatim into each of them — several tables that had to be
edited together and disagreed silently if they were not.

Times are New York, the clock every session window in this project is written
in.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Tuple

# session -> (open time, the session that opens next)
SESSIONS: Dict[str, Tuple[str, str]] = {
    "ASIA":     ("19:00", "LONDON"),
    "LONDON":   ("03:00", "NEW_YORK"),
    "NEW_YORK": ("09:30", "ASIA"),
}

SESSION_ORDER: List[str] = ["ASIA", "LONDON", "NEW_YORK"]

# A session normally trades until the next one opens. New York is the exception:
# Asia opens at 19:00, but the futures contract rolls at 18:00 — the CME open —
# so trading to 19:00 would leave a position sitting across the instrument
# change and book the whole calendar spread as a move that never happened.
# 16:55 also stops before the 17:00 COMEX halt, after which no tick arrives to
# trigger a close, so a position would ride the 17:00-18:00 break (and the whole
# weekend on a Friday).
SESSION_STOP_OVERRIDE: Dict[str, str] = {"NEW_YORK": "16:55"}


def add_minutes(hhmm: str, minutes: int) -> str:
    """"03:00" + 15 -> "03:15", wrapping at midnight."""
    return (datetime.strptime(hhmm, "%H:%M")
            + timedelta(minutes=int(minutes))).strftime("%H:%M")


def open_time(session: str) -> str:
    return SESSIONS[_key(session)][0]


def next_session(session: str) -> str:
    """Which session opens after this one. `stop_time` below is the usual way
    to ask; this is the raw lookup, kept because the wrap-around (New York ->
    Asia) is easy to get wrong by hand."""
    return SESSIONS[_key(session)][1]


def stop_time(session: str) -> str:
    """When this session must be flat: the next session's open, unless the
    session has an earlier hard stop of its own."""
    key = _key(session)
    return SESSION_STOP_OVERRIDE.get(key, SESSIONS[SESSIONS[key][1]][0])


def range_window(session: str, orb_minutes: int) -> Tuple[str, str, str]:
    """(range_start, range_end, stop_time) for a session and an ORB length."""
    start = open_time(session)
    return start, add_minutes(start, orb_minutes), stop_time(session)


def _key(session: str) -> str:
    key = str(session or "").strip().upper().replace(" ", "_").replace("-", "_")
    if key not in SESSIONS:
        raise ValueError(
            f"Unknown session '{session}'. Known sessions: "
            f"{', '.join(SESSION_ORDER)}.")
    return key
