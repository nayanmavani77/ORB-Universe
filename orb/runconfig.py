"""One master config per engine — and how several of them run together.

Each engine owns exactly one config file:

    orb/engines/orb/config.yaml
    orb/engines/orb_reverse/config.yaml

That file is COMPLETE. Instrument, data, account, fill assumptions, news dates,
sessions, the engine's own options, and the sweep grid all live in it. There is
no parent config to look up, and nothing to cross-reference: open one file and
you can see everything that run will use.

Mixed runs
----------
Running two engines against one account is still possible, and it does not need
a parent file. Name several engines and their SESSIONS are merged:

    python tools/backtest.py --engine orb,orb_reverse

Sessions come from each engine's own config; every session runs the engine
whose file it was written in, unless it says otherwise. The result is one
`AppConfig`, one broker, one equity curve — exactly what a single-file config
produces.

The shared settings must AGREE
------------------------------
A merged run has one account, one instrument and one data feed — physically
there is only one. So the blocks that describe those (`symbol`, `databento`,
`mt5`, the clock, and the account half of `backtest`) must match across the
files being merged. They are not silently taken from the first file: a mismatch
is an error naming the field and both values, because it means one of the two
configs is wrong about the world, and picking a winner would hide that.

Fields that are legitimately per-run — the output folder and report name — are
excluded from the check and decided by the tool.
"""
from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .config import AppConfig, StrategyConfig
from .markets import SESSION_ORDER, range_window
from .registry import names as engine_names
from .registry import settings_for, spec

#: the file every engine folder must contain
CONFIG_NAME = "config.yaml"

# blocks that describe the one account / one market a merged run shares
SHARED_BLOCKS = ("symbol", "databento", "mt5")
SHARED_SCALARS = ("server_timezone", "server_utc_offset_hours")
# per-run, so excluded from the agreement check
BACKTEST_PER_RUN = {"out_dir", "report_name"}

# how an engine config spells the news filter, and what the core calls it
NEWS_WORDS = {"skip": "off", "include": "on", "off": "off", "on": "on",
              "only": "only"}
NEWS_LABELS = {"off": "SKIP_NEWS", "on": "INCLUDE_NEWS", "only": "ONLY_NEWS"}


def engine_dir(engine: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "engines",
                        str(engine).strip().lower())


def config_path(engine: str) -> str:
    return os.path.join(engine_dir(engine), CONFIG_NAME)


def news_mode(word: str, where: str = "config") -> str:
    key = str(word).strip().lower()
    if key not in NEWS_WORDS:
        raise ValueError(
            f"{where}: news must be one of {', '.join(sorted(NEWS_WORDS))} "
            f"(got {word!r}).")
    return NEWS_WORDS[key]


def _read(path: str) -> Dict[str, Any]:
    try:
        import yaml
    except ImportError:                                   # pragma: no cover
        raise SystemExit("Reading a config needs PyYAML — `pip install pyyaml`")
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh.read()) or {}


# ==========================================================================
class RunConfig:
    """One engine's master config file, parsed.

    `app` is a fully-built `AppConfig` — the same object a single-file config
    has always produced, so every runner and tool downstream is unchanged.
    """

    def __init__(self, engine: str, path: str, app: AppConfig,
                 raw: Dict[str, Any]):
        self.engine = engine
        self.path = path
        self.app = app
        self.raw = raw
        self.period: Dict[str, Any] = dict(raw.get("period") or {})
        self.sweep: Dict[str, Any] = dict(raw.get("sweep") or {})

    # ------------------------------------------------------------------
    @staticmethod
    def load(engine: str, path: Optional[str] = None) -> "RunConfig":
        engine = str(engine).strip().lower()
        path = path or config_path(engine)
        if not os.path.exists(path):
            raise SystemExit(
                f"Engine '{engine}' has no {CONFIG_NAME}.\n"
                f"Expected it at: {path}\n"
                f"Every engine keeps exactly one config file, in its own "
                f"folder, and that file is the complete configuration for it.")
        raw = _read(path)
        declared = str(raw.get("engine") or engine).strip().lower()
        if declared != engine:
            raise SystemExit(
                f"{path} says `engine: {declared}` but it lives in the "
                f"'{engine}' folder. One config per engine, naming its own.")

        app = AppConfig.from_dict(raw)
        # every session runs this file's engine unless it names another
        for name, session in app.sessions.items():
            written = raw.get("sessions", {}).get(name, {}) if isinstance(
                raw.get("sessions"), dict) else {}
            if not (isinstance(written, dict) and written.get("engine")):
                session.engine = declared
        app.validate_sessions()

        rc = RunConfig(declared, path, app, raw)
        rc.validate()
        return rc

    @staticmethod
    def load_many(engines: Sequence[str]) -> List["RunConfig"]:
        return [RunConfig.load(e) for e in engines]

    # ------------------------------------------------------------------
    def validate(self) -> None:
        try:
            for word in (self.sweep.get("news") or []):
                news_mode(word, f"{self.path}: sweep")
            for session in self.app.enabled_sessions():
                settings_for(session.engine, session.engine_options)
        except ValueError as exc:
            raise SystemExit(f"{self.path}: {exc}") from exc
        for key, value in self.sweep.items():
            if key in ("out_dir", "save_trades"):
                continue
            if value is not None and not isinstance(value, (list, tuple)):
                raise SystemExit(
                    f"{self.path}: sweep.{key} must be a list, e.g. [0.5, 1.0] "
                    f"(got {value!r}).")

    # ------------------------------------------------------------------
    def dates(self) -> tuple:
        return (str(self.period.get("start", "2026-01-01")),
                str(self.period.get("end", "2026-08-13")))

    def data_paths(self) -> List[str]:
        data = self.period.get("data")
        if not data:
            paths = self.app.backtest.dbn_paths
            return paths if isinstance(paths, list) else [paths]
        return data if isinstance(data, list) else [data]

    def out_dir(self) -> Optional[str]:
        value = self.period.get("out_dir")
        return str(value) if value else None

    def sweep_out_dir(self) -> Optional[str]:
        value = self.sweep.get("out_dir")
        return str(value) if value else None

    def settings(self):
        """The settings of this file's first enabled session — the one a
        single-session research run means."""
        session = next(iter(self.app.enabled_sessions()), None)
        if session is None:
            return None
        return settings_for(session.engine, session.engine_options)

    # ------------------------------------------------------------------
    def app_config(self, overrides: Optional[Dict[str, Any]] = None) -> AppConfig:
        """This engine's config, with the usual research overrides applied to
        its enabled session(s)."""
        import copy
        o = {k: v for k, v in (overrides or {}).items() if v is not None}
        app = copy.deepcopy(self.app)
        app.backtest.dbn_paths = self.data_paths()

        sessions = app.enabled_sessions()
        if o and len(sessions) == 1:
            _apply_run_overrides(sessions[0], o, self.path)
        elif o:
            for session in sessions:
                _apply_run_overrides(session, o, self.path, window=False)
        app.validate_sessions()
        return app

    def run_name(self, overrides: Optional[Dict[str, Any]] = None) -> str:
        app = self.app_config(overrides)
        sessions = app.enabled_sessions()
        if len(sessions) != 1:
            return f"{self.engine}_{len(sessions)}sessions"
        s = sessions[0]
        minutes = _window_minutes(s.range_start, s.range_end)
        parts = [s.signal_timeframe, (s.name or "MAIN").upper(),
                 f"ORB{minutes}", f"RR{s.risk_reward:g}"]
        settings = settings_for(s.engine, s.engine_options)
        tail = getattr(settings, "run_name", None)
        if callable(tail):
            try:
                parts.append(tail(s.max_trades_per_session))
            except TypeError:
                parts.append(tail())
        return "_".join(parts).replace(".", "p")

    # ------------------------------------------------------------------
    def sweep_items(self, overrides: Optional[Dict[str, Any]] = None):
        """The grid described by `sweep:`, built by this engine's own
        `grid.build()`."""
        import copy
        import importlib
        w = dict(self.sweep)
        w.update({k: v for k, v in (overrides or {}).items() if v is not None})

        base = copy.deepcopy(self.app)
        base.backtest.dbn_paths = self.data_paths()

        module = spec(self.engine).strategy_cls.__module__.rsplit(".", 1)[0]
        grid = importlib.import_module(f"{module}.grid")
        kwargs: Dict[str, Any] = {}
        for key, value in w.items():
            if key in ("out_dir", "save_trades") or value is None:
                continue
            if key == "news":
                kwargs["news_modes"] = [NEWS_LABELS[news_mode(x, "sweep")]
                                        for x in value]
            elif key == "sessions":
                kwargs["sessions"] = [str(x).upper() for x in value]
            else:
                kwargs[key] = list(value)
        return grid.build(base, **kwargs)

    def sweep_size(self, overrides: Optional[Dict[str, Any]] = None) -> int:
        w = dict(self.sweep)
        w.update({k: v for k, v in (overrides or {}).items() if v is not None})
        total = 1
        for key, value in w.items():
            if key in ("out_dir", "save_trades") or value is None:
                continue
            total *= max(1, len(value))
        return total

    # ------------------------------------------------------------------
    def describe(self, overrides: Optional[Dict[str, Any]] = None) -> str:
        app = self.app_config(overrides)
        start, end = self.dates()
        lines = [f"  engine         {self.engine}",
                 f"  config file    {os.path.relpath(self.path)}",
                 f"  period         {start} .. {end}"]
        for s in app.enabled_sessions():
            settings = settings_for(s.engine, s.engine_options)
            lines.append(
                f"  session        {(s.name or 'MAIN'):<10} [{s.engine}]  "
                f"{s.range_start}-{s.range_end} -> {s.stop_time}  "
                f"{s.signal_timeframe}  R:R 1:{s.risk_reward:g}")
            if settings is not None:
                lines.append(f"                 {settings.describe()}")
        return "\n".join(lines)


# ==========================================================================
def merge(configs: Iterable[RunConfig]) -> AppConfig:
    """One `AppConfig` from several engines' master configs.

    The sessions of every engine, running together on one account. The shared
    blocks must agree — see the module docstring for why a mismatch is an error
    rather than a silent pick.
    """
    import copy
    configs = list(configs)
    if not configs:
        raise ValueError("merge() needs at least one config.")
    if len(configs) == 1:
        return configs[0].app_config()

    first = configs[0]
    _assert_shared_blocks_agree(configs)

    merged = copy.deepcopy(first.app)
    merged.sessions = {}
    seen_magic: Dict[int, str] = {}
    for rc in configs:
        for name, session in rc.app.sessions.items():
            if not session.enabled:
                continue
            key = name if name not in merged.sessions else f"{rc.engine}_{name}"
            clone = copy.deepcopy(session)
            clone.name = key
            if clone.magic in seen_magic:
                raise SystemExit(
                    f"Sessions '{seen_magic[clone.magic]}' and '{key}' both use "
                    f"magic {clone.magic}. Every session needs its own magic "
                    f"number so the broker can tell their positions apart — "
                    f"change one in its engine's {CONFIG_NAME}.")
            seen_magic[clone.magic] = key
            merged.sessions[key] = clone

    if not merged.sessions:
        raise SystemExit(
            "No enabled sessions in " +
            ", ".join(os.path.relpath(rc.path) for rc in configs))

    merged.backtest.dbn_paths = first.data_paths()
    merged.validate_sessions()
    return merged


def merged_dates(configs: Sequence[RunConfig]) -> tuple:
    """The widest period the merged configs agree to cover."""
    starts, ends = zip(*(rc.dates() for rc in configs))
    return min(starts), max(ends)


def _assert_shared_blocks_agree(configs: Sequence[RunConfig]) -> None:
    first, *rest = configs
    problems: List[str] = []
    for rc in rest:
        for block in SHARED_BLOCKS:
            a, b = asdict(getattr(first.app, block)), asdict(getattr(rc.app, block))
            for field in sorted(set(a) | set(b)):
                if a.get(field) != b.get(field):
                    problems.append(
                        f"  {block}.{field}: "
                        f"{os.path.relpath(first.path)} has {a.get(field)!r}, "
                        f"{os.path.relpath(rc.path)} has {b.get(field)!r}")
        for field in SHARED_SCALARS:
            if getattr(first.app, field) != getattr(rc.app, field):
                problems.append(
                    f"  {field}: "
                    f"{os.path.relpath(first.path)} has "
                    f"{getattr(first.app, field)!r}, "
                    f"{os.path.relpath(rc.path)} has {getattr(rc.app, field)!r}")
        a, b = asdict(first.app.backtest), asdict(rc.app.backtest)
        for field in sorted(set(a) | set(b)):
            if field in BACKTEST_PER_RUN:
                continue
            if a.get(field) != b.get(field):
                problems.append(
                    f"  backtest.{field}: "
                    f"{os.path.relpath(first.path)} has {a.get(field)!r}, "
                    f"{os.path.relpath(rc.path)} has {b.get(field)!r}")
    if problems:
        raise SystemExit(
            "These engines cannot run together — their configs disagree about "
            "things a single run has only one of:\n"
            + "\n".join(problems)
            + "\n\nOne account, one instrument, one data feed. Make the values "
              "match in the engine config files listed above.")


# --------------------------------------------------------------------------
def _window_minutes(start: str, end: str) -> int:
    def mins(hhmm: str) -> int:
        h, m = (int(x) for x in str(hhmm).split(":"))
        return h * 60 + m
    return (mins(end) - mins(start)) % 1440


def _apply_run_overrides(session: StrategyConfig, o: Dict[str, Any],
                         where: str, window: bool = True) -> None:
    """The research knobs the tools expose as flags."""
    if window and (o.get("session") or o.get("orb_minutes")):
        market = str(o.get("session") or session.name or "LONDON").upper()
        if market not in SESSION_ORDER:
            raise SystemExit(
                f"{where}: --session must be one of {', '.join(SESSION_ORDER)} "
                f"(got {market}).")
        minutes = int(o.get("orb_minutes")
                      or _window_minutes(session.range_start, session.range_end))
        start, end, stop = range_window(market, minutes)
        session.name = market.lower()
        session.range_start, session.range_end, session.stop_time = start, end, stop
    if o.get("signal_timeframe"):
        session.signal_timeframe = str(o["signal_timeframe"])
    if o.get("risk_reward") is not None:
        session.risk_reward = float(o["risk_reward"])
    if o.get("lots") is not None:
        session.lots = float(o["lots"])
    if o.get("log_level"):
        session.log_level = str(o["log_level"])
    if o.get("news"):
        mode = news_mode(o["news"], where)
        for _key, _label, cat in session.news.items():
            cat.mode = mode
        session.news_days, session.news_trading = "", mode
    if o.get("engine_options"):
        session.engine_options = dict(session.engine_options or {})
        session.engine_options.update(o["engine_options"])
        settings = settings_for(session.engine, session.engine_options)
        applier = getattr(settings, "apply_to_session", None)
        if callable(applier):
            applier(session)


def available() -> List[str]:
    """Engines that have a config file."""
    return [name for name in engine_names() if os.path.exists(config_path(name))]
