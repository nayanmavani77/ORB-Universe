"""Logging that reproduces the EA's journal format and behaviour.

* levels: 0 = errors only, 1 = normal, 2 = verbose            (ENUM_LOGLEVEL)
* identical consecutive lines are suppressed                  (g_lastLogLine)
* `info_once(key, msg)` logs at most once per session          (LogInfoOnce)
"""
from __future__ import annotations

import sys
from typing import Optional, Set, TextIO

LOG_NONE = 0
LOG_NORMAL = 1
LOG_VERBOSE = 2

_LEVEL_NAMES = {"none": 0, "normal": 1, "verbose": 2}


def parse_log_level(value) -> int:
    if isinstance(value, int):
        return max(0, min(2, value))
    return _LEVEL_NAMES.get(str(value).strip().lower(), LOG_NORMAL)


class RbeaLogger:
    """[RBEA] TAG | message"""

    def __init__(self, level: int = LOG_NORMAL, stream: Optional[TextIO] = None,
                 enabled: bool = True, file_path: Optional[str] = None,
                 show_time: bool = False):
        self.level = parse_log_level(level)
        self.stream = stream or sys.stdout
        self.enabled = enabled          # False == MQL_OPTIMIZATION (silent)
        self.show_time = show_time
        self._last_line = ""
        self._once_keys: Set[str] = set()
        self._fh = open(file_path, "a", encoding="utf-8") if file_path else None
        self.clock_time = None          # set by the engine so lines can be stamped

    # -- session guard ----------------------------------------------------
    def reset_once_keys(self) -> None:
        self._once_keys.clear()

    def first_time_this_session(self, key: str) -> bool:
        if key in self._once_keys:
            return False
        self._once_keys.add(key)
        return True

    # -- core -------------------------------------------------------------
    def write(self, level: int, tag: str, msg: str) -> None:
        if not self.enabled:
            return
        if level > 0 and self.level < level:
            return
        line = f"[RBEA] {tag} | {msg}"
        if line == self._last_line:      # no spam from repeated identical events
            return
        self._last_line = line
        if self.show_time and self.clock_time is not None:
            line = f"{self.clock_time:%Y.%m.%d %H:%M}  {line}"
        print(line, file=self.stream)
        if self._fh:
            self._fh.write(line + "\n")
            self._fh.flush()

    def error(self, msg: str) -> None:
        self.write(0, "ERROR", msg)

    def warn(self, msg: str) -> None:
        self.write(1, "WARN ", msg)

    def info(self, msg: str) -> None:
        self.write(1, "INFO ", msg)

    def debug(self, msg: str) -> None:
        self.write(2, "DEBUG", msg)

    def info_once(self, key: str, msg: str) -> None:
        if self.first_time_this_session(key):
            self.info(msg)

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
