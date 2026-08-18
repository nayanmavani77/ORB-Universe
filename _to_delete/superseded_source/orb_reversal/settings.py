"""Loader for `reversal_config.yaml`.

Turns the file into the three things the tools need:

    cfg.app_config()   an AppConfig with the single session already built
    cfg.settings()     a ReversalSettings
    cfg.sweep_items()  the grid for the sweep

The file inherits the instrument, the data paths, the news DATE lists, the
account size and the fill assumptions from the original `config.yaml` — one
place for those, and it stays the original place. Only the session window,
timeframe, R:R, stop, direction and cap are replaced.

Nothing here writes to `config.yaml` or to anything under `orb/`.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from orb.config import AppConfig
from orb.engines.reversal import ReversalSettings
from orb.engines.reversal.grid import build as build_grid
from orb.markets import SESSIONS, range_window

DEFAULT_PATH = "reversal_config.yaml"

_NEWS = {"skip": "off", "include": "on", "off": "off", "on": "on"}


def _read(path: str) -> Dict[str, Any]:
    try:
        import yaml
    except ImportError:                                  # pragma: no cover
        raise SystemExit("reversal_config.yaml needs PyYAML — "
                         "`pip install pyyaml`")
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh.read()) or {}


def _news_mode(value: str, where: str) -> str:
    key = str(value).strip().lower()
    if key not in _NEWS:
        raise ValueError(
            f"{where}: news must be 'skip' or 'include' (got {value!r}).")
    return _NEWS[key]


@dataclass
class ReversalConfig:
    """`reversal_config.yaml`, parsed and validated."""
    path: str = DEFAULT_PATH
    base_config: str = "config.yaml"
    run: Dict[str, Any] = field(default_factory=dict)
    reversal: Dict[str, Any] = field(default_factory=dict)
    period: Dict[str, Any] = field(default_factory=dict)
    output: Dict[str, Any] = field(default_factory=dict)
    sweep: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    @staticmethod
    def load(path: str = DEFAULT_PATH) -> "ReversalConfig":
        if not os.path.exists(path):
            raise SystemExit(
                f"No reversal configuration at '{path}'.\n"
                f"It ships with the project as 'reversal_config.yaml'. Pass "
                f"--reversal-config to point somewhere else.")
        raw = _read(path)
        cfg = ReversalConfig(
            path=path,
            base_config=raw.get("base_config") or "config.yaml",
            run=dict(raw.get("run") or {}),
            reversal=dict(raw.get("reversal") or {}),
            period=dict(raw.get("period") or {}),
            output=dict(raw.get("output") or {}),
            sweep=dict(raw.get("sweep") or {}),
        )
        cfg.validate()
        return cfg

    # ------------------------------------------------------------------
    def validate(self) -> None:
        base = self.base_config
        if not os.path.isabs(base):
            near = os.path.join(os.path.dirname(os.path.abspath(self.path)), base)
            if os.path.exists(base) or os.path.exists(near):
                pass
            else:
                raise SystemExit(
                    f"{self.path}: base_config '{base}' not found. It should "
                    f"name your original config file, normally 'config.yaml'.")
        s = str(self.run.get("session", "LONDON")).upper()
        if s not in SESSIONS:
            raise SystemExit(
                f"{self.path}: run.session must be one of "
                f"{', '.join(SESSIONS)} (got {self.run.get('session')!r}).")
        try:
            _news_mode(self.run.get("news", "skip"), f"{self.path}: run")
            for n in (self.sweep.get("news") or []):
                _news_mode(n, f"{self.path}: sweep")
            self.settings().validate()
        except ValueError as exc:
            raise SystemExit(f"{self.path}: {exc}") from exc
        for key in ("sessions", "timeframes", "orb_minutes", "risk_reward",
                    "sl_range_mult", "max_trades", "directions", "news"):
            v = self.sweep.get(key)
            if v is not None and not isinstance(v, (list, tuple)):
                raise SystemExit(f"{self.path}: sweep.{key} must be a list, "
                                 f"e.g. [0.5, 1.0] (got {v!r}).")

    # ------------------------------------------------------------------
    def base_path(self) -> str:
        if os.path.exists(self.base_config):
            return self.base_config
        return os.path.join(os.path.dirname(os.path.abspath(self.path)),
                            self.base_config)

    def settings(self) -> ReversalSettings:
        r = self.reversal
        return ReversalSettings(
            sl_range_mult=float(r.get("sl_range_mult", 0.5)),
            direction=str(r.get("direction", "reverse")).lower(),
            sl_anchor=str(r.get("sl_anchor", "range")).lower(),
            max_trades_per_session=int(
                r.get("max_trades_per_session", r.get("max_trades", 0))),
            order_tag=str(r.get("order_tag", r.get("tag", "REV"))),
        )

    def dates(self) -> tuple:
        return (str(self.period.get("start", "2026-01-01")),
                str(self.period.get("end", "2026-08-13")))

    def data_paths(self, base: AppConfig) -> List[str]:
        d = self.period.get("data")
        if not d:
            paths = base.backtest.dbn_paths
            return paths if isinstance(paths, list) else [paths]
        return d if isinstance(d, list) else [d]

    def out_dir(self) -> str:
        return str(self.output.get("dir", "reversal_run"))

    def sweep_out_dir(self) -> str:
        return str(self.sweep.get("out_dir", "reversal_sweep"))

    def name(self) -> str:
        r, st = self.run, self.settings()
        tf = str(r.get("signal_timeframe", "M5"))
        return (f"{tf}_{str(r.get('session', 'LONDON')).upper()}"
                f"_ORB{int(r.get('orb_minutes', 15))}"
                f"_RR{float(r.get('risk_reward', 2.0)):g}"
                f"_SL{st.sl_range_mult:g}"
                f"_{'REV' if st.reverse else 'FWD'}").replace(".", "p")

    # ------------------------------------------------------------------
    def app_config(self, overrides: Optional[Dict[str, Any]] = None) -> AppConfig:
        """The base config with this file's single session applied on top."""
        o = overrides or {}
        r = dict(self.run)
        r.update({k: v for k, v in o.items() if v is not None})

        app = AppConfig.load(self.base_path())
        session = str(r.get("session", "LONDON")).upper()
        orb = int(r.get("orb_minutes", 15))
        start, end, stop = range_window(session, orb)

        s = app.use_single_session(self.name())
        s.signal_timeframe = str(r.get("signal_timeframe", "M5"))
        s.range_start = r.get("range_start") or start
        s.range_end = r.get("range_end") or end
        s.stop_time = r.get("stop_time") or stop
        s.risk_reward = float(r.get("risk_reward", 2.0))
        s.lots = float(r.get("lots", s.lots))
        s.log_level = str(r.get("log_level", "normal"))

        mode = _news_mode(r.get("news", "skip"), "run")
        for _k, _l, cat in s.news.items():
            cat.mode = mode
        s.news_days, s.news_trading = "", mode

        app.server_timezone = "America/New_York"
        app.server_utc_offset_hours = 0
        app.backtest.dbn_paths = self.data_paths(app)
        # stamp the reversal settings on, so the returned config fully describes
        # what will run. `run_reversal` re-applies them to its own copy, which
        # is idempotent.
        self.settings().apply_to(app)
        return app

    # ------------------------------------------------------------------
    def sweep_items(self, overrides: Optional[Dict[str, Any]] = None):
        """The grid described by section 5, ready to run."""
        o = overrides or {}
        w = dict(self.sweep)
        w.update({k: v for k, v in o.items() if v is not None})

        base = AppConfig.load(self.base_path())
        base.backtest.dbn_paths = self.data_paths(base)
        base.server_timezone = "America/New_York"
        base.server_utc_offset_hours = 0

        news_labels = [("SKIP_NEWS" if _news_mode(n, "sweep") == "off"
                        else "INCLUDE_NEWS") for n in w.get("news",
                                                            ["include", "skip"])]
        return build_grid(
            base,
            sessions=[str(x).upper() for x in w.get("sessions", ["LONDON"])],
            timeframes=[str(x).upper() for x in w.get("timeframes",
                                                      ["M1", "M5", "M15"])],
            orb_minutes=[int(x) for x in w.get("orb_minutes", [15, 30, 60])],
            news_modes=news_labels,
            risk_reward=[float(x) for x in w.get("risk_reward",
                                               [1, 1.5, 2, 2.5, 3])],
            sl_range_mults=[float(x) for x in w.get("sl_range_mult",
                                                    [0.25, 0.5, 0.75, 1, 1.5, 2])],
            trade_caps=[int(x) for x in w.get("max_trades", [1, 2, 3])],
            directions=[str(x).lower() for x in w.get("directions",
                                                      ["reverse", "forward"])],
            sl_anchor=self.settings().sl_anchor,
        )

    def sweep_size(self, overrides: Optional[Dict[str, Any]] = None) -> int:
        w = dict(self.sweep)
        w.update({k: v for k, v in (overrides or {}).items() if v is not None})
        n = 1
        for key, default in (("sessions", ["LONDON"]),
                             ("timeframes", ["M1", "M5", "M15"]),
                             ("orb_minutes", [15, 30, 60]),
                             ("news", ["include", "skip"]),
                             ("risk_reward", [1, 1.5, 2, 2.5, 3]),
                             ("sl_range_mult", [0.25, 0.5, 0.75, 1, 1.5, 2]),
                             ("max_trades", [1, 2, 3]),
                             ("directions", ["reverse", "forward"])):
            n *= len(w.get(key, default))
        return n

    # ------------------------------------------------------------------
    def describe(self) -> str:
        r, st = self.run, self.settings()
        start, end = self.dates()
        return "\n".join([
            f"  config file    {self.path}",
            f"  inherits       {self.base_config}",
            f"  session        {str(r.get('session','LONDON')).upper()}"
            f"  ORB {int(r.get('orb_minutes',15))} min  TF "
            f"{r.get('signal_timeframe','M5')}",
            f"  risk:reward    1:{float(r.get('risk_reward',2.0)):g}",
            f"  stop loss      {st.sl_range_mult:g} x opening range"
            + {0.5: "   (= the original mid_range)",
               1.0: "   (= the original full_range)"}.get(
                   float(st.sl_range_mult), ""),
            f"  direction      {'REVERSE (fade)' if st.reverse else 'FORWARD (control arm)'}",
            f"  per session    " + (
                f"first {st.max_trades_per_session} trade(s)"
                if st.max_trades_per_session else "unlimited"),
            f"  news           {r.get('news','skip')}",
            f"  period         {start} .. {end}",
        ])
