"""The journal: what the EA did, and when, readable at a glance.

    ---- 2026-08-18 ------------------------------------------------------
    09:32:00  INFO   new_york  | Session opened. Range window 09:32-09:33
    09:34:01  INFO   new_york  | Range built | high 4451.600  low 4447.600
    09:35:01  INFO   new_york  | BREAKOUT UP | bar 09:34 closed 4452.500

Every line carries the SESSION it belongs to. With several engines running
side by side the journal is interleaved, and a line reading "New session" or
"Stop Time reached" with no owner is unreadable — you cannot tell which
session it is talking about. `for_session()` binds a name once and every line
from that strategy is tagged.

Timestamps carry SECONDS. The EA now acts within about a second of a bar
closing, and a minute-resolution stamp hid exactly the thing worth watching.
The date is printed once, as a banner, when it changes.

Behaviour carried over from the MQL5 EA:

* levels: 0 = errors only, 1 = normal, 2 = verbose            (ENUM_LOGLEVEL)
* identical consecutive lines are suppressed                  (g_lastLogLine)
* `info_once(key, msg)` logs at most once per session          (LogInfoOnce)
"""
from __future__ import annotations

import copy
import sys
from typing import List, Optional, Set, TextIO

LOG_NONE = 0
LOG_NORMAL = 1
LOG_VERBOSE = 2

_LEVEL_NAMES = {"none": 0, "normal": 1, "verbose": 2}


def parse_log_level(value) -> int:
    if isinstance(value, int):
        return max(0, min(2, value))
    return _LEVEL_NAMES.get(str(value).strip().lower(), LOG_NORMAL)


#: width of the session column. Long enough for "new_york"; anything longer is
#: truncated rather than allowed to break the alignment.
#: how much room the journal gives a session name. Wide enough for the
#: `<window>_<instrument>` names a session matrix produces — `new_york_gc` is
#: 11 — so the column no longer truncates the part that says which symbol.
SOURCE_WIDTH = 12


class RbeaLogger:
    """`HH:MM:SS  LEVEL  session  | message`"""

    def __init__(self, level: int = LOG_NORMAL, stream: Optional[TextIO] = None,
                 enabled: bool = True, file_path: Optional[str] = None,
                 show_time: bool = False, source: str = ""):
        self.level = parse_log_level(level)
        self.stream = stream or sys.stdout
        self.enabled = enabled          # False == MQL_OPTIMIZATION (silent)
        self.show_time = show_time
        #: which session this logger speaks for; blank for engine-wide lines
        self.source = source
        # Held in one-element lists so a session logger made by `for_session`
        # SHARES them: duplicate suppression and the date banner must be global
        # across sessions, or an interleaved journal repeats itself.
        self._last_line: List[str] = [""]
        self._last_date: List[str] = [""]
        self._once_keys: Set[str] = set()
        self._fh = open(file_path, "a", encoding="utf-8") if file_path else None
        self.clock_time = None          # set by the engine so lines can be stamped

    # -- one logger per session -------------------------------------------
    def for_session(self, name: str) -> "RbeaLogger":
        """A logger that tags every line with this session's name.

        Shares the file handle, the stream, the once-keys and the duplicate
        guard with its parent — it is the same journal, just labelled. Only
        `source` and `clock_time` are its own, so each session can stamp its
        own lines without disturbing another's.
        """
        child = copy.copy(self)
        child.source = str(name or "")
        return child

    # -- session guard ----------------------------------------------------
    def reset_once_keys(self, prefix: Optional[str] = None) -> None:
        """Forget the once-per-session keys.

        With `prefix`, only that owner's keys are cleared. Sessions share one
        logger in a multi-engine run, so an unqualified clear let one session
        starting a new day wipe another's suppression — and "Stop Time reached"
        reappeared for a session whose state had not changed.
        """
        if prefix is None:
            self._once_keys.clear()
            return
        for key in [k for k in self._once_keys if k.startswith(prefix)]:
            self._once_keys.discard(key)

    def first_time_this_session(self, key: str) -> bool:
        if key in self._once_keys:
            return False
        self._once_keys.add(key)
        return True

    # -- core -------------------------------------------------------------
    def _emit(self, line: str) -> None:
        print(line, file=self.stream)
        if self._fh:
            self._fh.write(line + "\n")
            self._fh.flush()

    def write(self, level: int, tag: str, msg: str) -> None:
        if not self.enabled:
            return
        if level > 0 and self.level < level:
            return

        source = (self.source or "")[:SOURCE_WIDTH]
        body = f"{tag} {source:<{SOURCE_WIDTH}} | {msg}"
        if body == self._last_line[0]:   # no spam from repeated identical events
            return
        self._last_line[0] = body

        if not self.show_time:
            self._emit(body)
            return

        # Start-up lines are written before the engine has a clock, so they
        # have nothing to stamp. Pad them to the same width rather than let
        # them sit a column to the left — a journal whose pipe wanders is
        # exactly as hard to scan as one with no session names.
        if self.clock_time is None:
            self._emit(" " * len("00:00:00  ") + body)
            return

        # the date once, as a banner, instead of on every single line
        day = f"{self.clock_time:%Y-%m-%d}"
        if day != self._last_date[0]:
            self._last_date[0] = day
            self._emit(f"---- {day} " + "-" * 56)
        self._emit(f"{self.clock_time:%H:%M:%S}  {body}")

    def error(self, msg: str) -> None:
        self.write(0, "ERROR", msg)

    def warn(self, msg: str) -> None:
        self.write(1, "WARN ", msg)

    def info(self, msg: str) -> None:
        self.write(1, "INFO ", msg)

    def debug(self, msg: str) -> None:
        self.write(2, "DEBUG", msg)

    def banner(self, title: str, lines=()) -> None:
        """A boxed heading. For the few moments worth separating from the flow:
        start-up, and each engine's own description."""
        if not self.enabled or self.level < LOG_NORMAL:
            return
        rule = "=" * 68
        self._emit(rule)
        self._emit(f"  {title}")
        for extra in lines:
            self._emit(f"    {extra}")
        self._emit(rule)

    def info_once(self, key: str, msg: str) -> None:
        if self.first_time_this_session(key):
            self.info(msg)

    def warn_once(self, key: str, msg: str) -> None:
        """As `info_once`, at WARN. For a condition that should be seen but
        would otherwise repeat on every tick."""
        if self.first_time_this_session(key):
            self.warn(msg)

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
