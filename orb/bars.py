"""Bar container, timeframe resampler and the bar store used by ComputeRange."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, List, Optional

from .timeutils import SECONDS_PER_DAY


@dataclass
class Bar:
    """One OHLCV bar.  `time` is the bar OPEN time in *server* time."""
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    #: which instrument this bar describes. Empty means "the only one", which
    #: is what every single-instrument run produces, so nothing that predates
    #: multi-instrument support has to change. `MultiEngine` routes on it.
    instrument: str = ""

    def end_time(self, tf_seconds: int) -> datetime:
        return self.time + timedelta(seconds=tf_seconds)


def bucket_start(t: datetime, tf_seconds: int) -> datetime:
    """MT5-style bar alignment: intraday bars are aligned to the server-time day.

    M5 buckets start at 00:00, 00:05, ...; H4 at 00:00, 04:00, ...; D1 at 00:00.
    """
    midnight = datetime(t.year, t.month, t.day)
    if tf_seconds >= SECONDS_PER_DAY:
        return midnight
    secs = (t - midnight).total_seconds()
    return midnight + timedelta(seconds=(int(secs) // tf_seconds) * tf_seconds)


class Resampler:
    """Aggregates base-resolution bars into signal-timeframe bars.

    `push(bar)` returns the *previous* timeframe bar at the moment it is
    completed by the arrival of a bar belonging to a new bucket — this is the
    exact analogue of MT5's `iTime(_Symbol, tf, 0)` changing value, which is
    what the EA uses to detect a freshly closed bar.
    """

    def __init__(self, tf_seconds: int):
        self.tf_seconds = int(tf_seconds)
        self._cur: Optional[Bar] = None

    @property
    def current(self) -> Optional[Bar]:
        return self._cur

    def push(self, bar: Bar) -> Optional[Bar]:
        bs = bucket_start(bar.time, self.tf_seconds)
        if self._cur is None:
            self._cur = Bar(bs, bar.open, bar.high, bar.low, bar.close, bar.volume)
            return None
        if bs == self._cur.time:
            self._cur.high = max(self._cur.high, bar.high)
            self._cur.low = min(self._cur.low, bar.low)
            self._cur.close = bar.close
            self._cur.volume += bar.volume
            return None
        closed = self._cur
        self._cur = Bar(bs, bar.open, bar.high, bar.low, bar.close, bar.volume)
        return closed

    def flush(self) -> Optional[Bar]:
        closed, self._cur = self._cur, None
        return closed


class BarStore:
    """Rolling store of completed timeframe bars.

    `window(start, end)` reproduces `CopyRates(sym, tf, start, end - 1s)`:
    bars whose OPEN time lies in [start, end).
    """

    def __init__(self, keep_days: int = 8):
        self.bars: List[Bar] = []
        self.keep = timedelta(days=keep_days)

    def add(self, bar: Bar) -> None:
        self.bars.append(bar)
        if self.bars and (self.bars[-1].time - self.bars[0].time) > self.keep:
            cutoff = self.bars[-1].time - self.keep
            i = 0
            while i < len(self.bars) and self.bars[i].time < cutoff:
                i += 1
            if i:
                del self.bars[:i]

    def window(self, start: datetime, end: datetime) -> List[Bar]:
        return [b for b in self.bars if start <= b.time < end]

    def extend(self, bars: Iterable[Bar]) -> None:
        for b in bars:
            self.add(b)
