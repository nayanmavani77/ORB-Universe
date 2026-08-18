"""Time helpers — direct equivalents of the MQL5 helper functions.

The EA works entirely in *broker server time*.  In this port every bar
timestamp is converted from UTC to server time once, at ingest, so all
downstream logic (session windows, stop time, skip dates) sees exactly the
same clock the EA sees.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional, Tuple

try:  # py>=3.9
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

SECONDS_PER_DAY = 86400


# --------------------------------------------------------------------------
# HH:MM parsing  (MQL5: ParseHHMM)
# --------------------------------------------------------------------------
def parse_hhmm(s: str) -> Tuple[int, bool]:
    """Return (seconds_from_midnight, disabled).

    "", "0", "00:00", "0:00", "0000" all mean *disabled*, exactly as in the EA.
    Raises ValueError on malformed input.
    """
    s = (s or "").strip()
    if s in ("", "0", "00:00", "0:00", "0000"):
        return 0, True
    parts = s.split(":")
    if len(parts) < 2:
        raise ValueError(f"Invalid time '{s}'. Use HH:MM, e.g. 09:00")
    try:
        h = int(parts[0])
        m = int(parts[1])
    except ValueError:
        raise ValueError(f"Invalid time '{s}'. Use HH:MM, e.g. 09:00")
    if not (0 <= h <= 23) or not (0 <= m <= 59):
        raise ValueError(f"Invalid time '{s}'. Use HH:MM, e.g. 09:00")
    return h * 3600 + m * 60, False


# --------------------------------------------------------------------------
# Date parsing / skip list  (MQL5: ParseDate, BuildSkipDates, IsSkippedDate)
# --------------------------------------------------------------------------
def parse_date(s: str) -> Optional[date]:
    """Parse 'YYYY.MM.DD' (also accepts / and - separators)."""
    s = (s or "").strip()
    if not s:
        return None
    s = s.replace("/", ".").replace("-", ".")
    p = s.split(".")
    if len(p) != 3:
        return None
    try:
        y, mo, d = int(p[0]), int(p[1]), int(p[2])
        if y < 1971 or not (1 <= mo <= 12) or not (1 <= d <= 31):
            return None
        return date(y, mo, d)
    except ValueError:
        return None


class NewsDays:
    """A list of dates supporting single dates and 'from-to' ranges.

    Used for the News Days filter. How the list is *applied* (trade on them,
    avoid them, or trade only them) is decided by `news_trading`.

    Parsing mirrors the EA: tokens separated by , or ;, whitespace removed,
    a range written as 2026.04.03-2026.04.06.  Reversed ranges are swapped.
    Unparseable tokens are warned about and ignored.
    """

    def __init__(self, src: str = "", warn=None):
        self.ranges: List[Tuple[date, date]] = []
        clean = (src or "").strip()
        if not clean:
            return
        # ; and any line break are separators just like a comma, so a long
        # list can be written one date per line in YAML, with or without
        # commas. A trailing comma or blank line is not an error.
        for ch in (";", "\n", "\r", "\t"):
            clean = clean.replace(ch, ",")
        clean = clean.replace(" ", "")
        for tk in clean.split(","):
            tk = tk.strip()
            if not tk:
                continue
            frm = to = None
            dash = tk.find("-", 1)
            # a range looks like 2026.04.03-2026.04.06
            if dash > 0 and len(tk) > dash + 1 and "." in tk and "." in tk[dash:]:
                frm = parse_date(tk[:dash])
                to = parse_date(tk[dash + 1:])
            else:
                frm = parse_date(tk)
                to = frm
            if frm is None or to is None:
                if warn:
                    warn(f'News Days: could not parse "{tk}" - entry ignored.')
                continue
            if to < frm:
                frm, to = to, frm
            self.ranges.append((frm, to))

    def __len__(self) -> int:
        return len(self.ranges)

    def contains(self, t: datetime) -> bool:
        d = t.date()
        return any(a <= d <= b for a, b in self.ranges)


# --------------------------------------------------------------------------
# Server clock
# --------------------------------------------------------------------------
class ServerClock:
    """Converts UTC timestamps to broker/server time.

    Two modes:
      * fixed offset  (``utc_offset_hours``)  — no DST, matches most MT5 servers'
        arithmetic exactly and reproduces the EA's `DateOnly()` day boundaries.
      * named zone    (``timezone_name``)     — DST-aware, e.g. "Europe/Athens".
    """

    def __init__(self, utc_offset_hours: Optional[float] = None,
                 timezone_name: Optional[str] = None):
        if timezone_name:
            if ZoneInfo is None:  # pragma: no cover
                raise RuntimeError("zoneinfo unavailable; use utc_offset_hours instead")
            self._tz = ZoneInfo(timezone_name)
            self._fixed = None
        else:
            self._tz = None
            self._fixed = timedelta(hours=float(utc_offset_hours or 0.0))

    # -- conversions ------------------------------------------------------
    def to_server(self, ts_utc: datetime) -> datetime:
        """UTC (aware or naive-UTC) -> naive server-local datetime."""
        if ts_utc.tzinfo is None:
            ts_utc = ts_utc.replace(tzinfo=timezone.utc)
        if self._tz is not None:
            return ts_utc.astimezone(self._tz).replace(tzinfo=None)
        return (ts_utc.astimezone(timezone.utc) + self._fixed).replace(tzinfo=None)

    def to_utc(self, ts_server: datetime) -> datetime:
        """Naive server-local datetime -> aware UTC datetime."""
        if self._tz is not None:
            return ts_server.replace(tzinfo=self._tz).astimezone(timezone.utc)
        return (ts_server - self._fixed).replace(tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.to_server(datetime.now(timezone.utc))


def date_only(t: datetime) -> datetime:
    """MQL5 DateOnly(): midnight of the day `t` falls in (server time)."""
    return datetime(t.year, t.month, t.day)


def fmt_dt(t: datetime) -> str:
    """MQL5 TimeToString(t, TIME_DATE|TIME_MINUTES)."""
    return t.strftime("%Y.%m.%d %H:%M")


def fmt_time(t: datetime) -> str:
    """MQL5 TimeToString(t, TIME_MINUTES)."""
    return t.strftime("%H:%M")


def fmt_date(t: datetime) -> str:
    """MQL5 TimeToString(t, TIME_DATE)."""
    return t.strftime("%Y.%m.%d")


# --------------------------------------------------------------------------
# Timeframe helpers
# --------------------------------------------------------------------------
_TF_SECONDS = {
    "M1": 60, "M2": 120, "M3": 180, "M4": 240, "M5": 300, "M6": 360,
    "M10": 600, "M12": 720, "M15": 900, "M20": 1200, "M30": 1800,
    "H1": 3600, "H2": 7200, "H3": 10800, "H4": 14400, "H6": 21600,
    "H8": 28800, "H12": 43200, "D1": 86400,
}


def timeframe_seconds(tf: str) -> int:
    """MQL5 PeriodSeconds() for the MT5 timeframe names."""
    key = str(tf).strip().upper().replace("PERIOD_", "")
    if key in _TF_SECONDS:
        return _TF_SECONDS[key]
    # allow plain seconds / "5m" style
    if key.endswith("M") and key[:-1].isdigit():
        return int(key[:-1]) * 60
    if key.isdigit():
        return int(key)
    raise ValueError(f"Unknown timeframe '{tf}'. Use M1..D1, e.g. M5.")


# backwards-compatible alias for the pre-rename name
SkipDates = NewsDays
