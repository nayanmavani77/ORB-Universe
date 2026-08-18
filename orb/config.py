"""Configuration — every MQL5 `input` has a 1:1 counterpart here."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

SECONDS_PER_DAY = 86400

try:
    import yaml  # optional
except Exception:  # pragma: no cover
    yaml = None


SL_MIDPOINT = "mid_range"
SL_FULL_RANGE = "full_range"

# --- News Days handling ---------------------------------------------------
NEWS_ON = "on"      # trade every day, News Days included (the list is ignored)
NEWS_OFF = "off"    # never trade on a News Day
NEWS_ONLY = "only"  # trade ONLY on News Days
NEWS_MODES = (NEWS_ON, NEWS_OFF, NEWS_ONLY)

# The eight tracked news categories: config key -> (human label, CLI prefix)
NEWS_CATEGORIES = {
    "core_cpi_mm":                ("Core CPI m/m",               "cpi"),
    "unemployment_rate":          ("Unemployment Rate",          "unemployment"),
    "non_farm_employment_change": ("Non-Farm Employment Change", "nfp"),
    "ism_manufacturing_pmi":      ("ISM Manufacturing PMI",      "ism-mfg"),
    "core_pce_price_index_mm":    ("Core PCE Price Index m/m",   "pce"),
    "federal_funds_rate":         ("Federal Funds Rate",         "fomc"),
    "core_ppi_mm":                ("Core PPI m/m",               "ppi"),
    "ism_services_pmi":           ("ISM Services PMI",           "ism-svc"),
}


@dataclass
class NewsCategory:
    """One news category: its dates, and how those dates are used."""
    mode: str = NEWS_OFF      # on | off | only
    dates: str = ""           # single dates and from-to ranges

    def validate(self, label: str) -> None:
        m = str(self.mode).strip().lower()
        if m not in NEWS_MODES:
            raise ValueError(
                f"{label}: mode must be one of {', '.join(NEWS_MODES)} "
                f"(got '{self.mode}').")
        self.mode = m
        if m == NEWS_ONLY and not str(self.dates).strip():
            raise ValueError(
                f"{label}: mode is 'only' but no dates are listed — that "
                f"category can never match, so the EA would never trade.")


@dataclass
class NewsConfig:
    """All eight categories. Each is independent."""
    core_cpi_mm: NewsCategory = field(default_factory=NewsCategory)
    unemployment_rate: NewsCategory = field(default_factory=NewsCategory)
    non_farm_employment_change: NewsCategory = field(default_factory=NewsCategory)
    ism_manufacturing_pmi: NewsCategory = field(default_factory=NewsCategory)
    core_pce_price_index_mm: NewsCategory = field(default_factory=NewsCategory)
    federal_funds_rate: NewsCategory = field(default_factory=NewsCategory)
    core_ppi_mm: NewsCategory = field(default_factory=NewsCategory)
    ism_services_pmi: NewsCategory = field(default_factory=NewsCategory)

    def items(self):
        """(key, human label, NewsCategory) for each category, in order."""
        for key, (label, _flag) in NEWS_CATEGORIES.items():
            yield key, label, getattr(self, key)


# ==========================================================================
# Strategy inputs (exact mirror of the EA's input block)
# ==========================================================================
@dataclass
class StrategyConfig:
    """One trading session's complete rule set.

    Every field here is per-session: run Asia on M1 with R:R 1.5 and New York
    on M15 with R:R 3 if you want to. The rules themselves never change — this
    is the same input block the EA has always had; the system just runs one
    instance of it per enabled session.
    """
    # === Session identity ===
    # `name` labels the session in the journal, the trades CSV and the report.
    # `enabled` is the ON/OFF switch — a disabled session is never constructed,
    # so it costs nothing and can take no trade.
    name: str = ""
    enabled: bool = True

    # === Which strategy this session runs ===
    # The name of a registered engine — see `orb/engines/`. Every session picks
    # its own, so Asia can run one strategy while London runs another, in the
    # same backtest and in the same live process.
    #
    # `engine_options` carries whatever that engine needs beyond the standard
    # fields below. Its contents are validated by the engine itself (each has a
    # `settings.py`), so this file never has to learn one strategy's vocabulary.
    # A plain dict is used deliberately: it survives `asdict()`, so options are
    # inherited by sessions and appear in `to_dict()` / `--show-config`.
    #
    #   sessions:
    #     london:
    #       engine: reversal
    #       engine_options:
    #         sl_range_mult: 0.75
    #         direction: reverse
    engine: str = "orb"
    engine_options: Dict[str, Any] = field(default_factory=dict)

    # === Session times (broker/server time) ===
    range_start: str = "09:00"            # InpRangeStart
    range_end: str = "10:00"              # InpRangeEnd
    stop_time: str = "17:00"              # InpStopTime  ("0" = never stop)
    signal_timeframe: str = "M5"          # InpSignalTF

    # === Trade settings ===
    sl_mode: str = SL_MIDPOINT            # InpSLMode
    risk_reward: float = 2.0              # InpRR
    lots: float = 0.10                    # InpLots
    require_range_reentry: bool = True    # InpRequireRangeReentry
    max_trades_per_session: int = 0       # InpMaxTradesPerSession (0 = unlimited)
    close_at_stop_time: bool = True       # InpCloseAtStopTime

    # === News Days ===
    # One switch for the whole news filter, per session. Set it and every
    # category — and the general bucket — takes this mode, whatever they say
    # individually. This is the short way to write "New York trades through
    # news, Asia sits it out":
    #
    #   sessions:
    #     asia:      { news_mode: "off" }   # skip every listed news date
    #     new_york:  { news_mode: "on"  }   # trade straight through them
    #
    # Leave it null to use each category's own mode.
    news_mode: Optional[str] = None
    # Per-category dates and modes (Core CPI m/m, NFP, FOMC, ...)
    news: NewsConfig = field(default_factory=NewsConfig)
    # An extra un-categorised bucket, kept so existing configs still work.
    # It behaves exactly like a ninth category.
    news_days: str = ""                   # InpNewsDays (was InpSkipDates)
    news_trading: str = NEWS_OFF          # InpNewsTrading: on | off | only

    # === Logging ===
    log_level: str = "normal"             # InpLogLevel
    log_file: Optional[str] = None
    log_show_time: bool = True            # prefix journal lines with server time

    # === Misc ===
    magic: int = 20260814                 # InpMagic
    comment: str = "RangeBreak"           # InpComment

    def validate(self) -> None:
        from .timeutils import parse_hhmm
        # The engine NAME is checked here; its OPTIONS are not. Validating the
        # options would mean this file knowing what every strategy's settings
        # mean, which is exactly the coupling `engine_options` exists to avoid.
        # The engine validates them itself when the strategy is built.
        self.engine = str(self.engine or "orb").strip().lower()
        if not self.engine:
            raise ValueError("engine cannot be empty — name a registered engine.")
        if self.engine_options is None:
            self.engine_options = {}
        if not isinstance(self.engine_options, dict):
            raise ValueError(
                f"engine_options must be a mapping of option names to values "
                f"(got {type(self.engine_options).__name__}).")
        s, dis = parse_hhmm(self.range_start)
        if dis:
            raise ValueError("Invalid Range Start Time. Use HH:MM, e.g. 09:00")
        e, dis = parse_hhmm(self.range_end)
        if dis:
            raise ValueError("Invalid Range End Time. Use HH:MM, e.g. 10:00")
        if s == e:
            raise ValueError("Range Start Time and Range End Time must differ.")
        parse_hhmm(self.stop_time)  # raises if malformed; "0" is allowed
        if self.sl_mode not in (SL_MIDPOINT, SL_FULL_RANGE):
            raise ValueError(f"sl_mode must be '{SL_MIDPOINT}' or '{SL_FULL_RANGE}'")
        if self.risk_reward <= 0:
            raise ValueError("Risk:Reward must be greater than 0.")
        if self.lots <= 0:
            raise ValueError("Lot size must be greater than 0.")
        # a session-wide news_mode overrides every individual category, so the
        # whole filter is one line per session
        if self.news_mode is not None:
            nm = str(self.news_mode).strip().lower()
            if nm not in NEWS_MODES:
                raise ValueError(
                    f"news_mode must be one of {', '.join(NEWS_MODES)} "
                    f"(got '{self.news_mode}').")
            self.news_mode = nm
            for _key, _label, cat in self.news.items():
                cat.mode = nm
            self.news_trading = nm
        mode = str(self.news_trading).strip().lower()
        if mode not in NEWS_MODES:
            raise ValueError(
                f"news_trading must be one of {', '.join(NEWS_MODES)} "
                f"(got '{self.news_trading}').")
        self.news_trading = mode
        # "only" with an empty list would silently take zero trades forever
        if mode == NEWS_ONLY and not str(self.news_days).strip():
            raise ValueError(
                "news_trading is 'only' but news_days is empty — the EA would "
                "never trade. List the News Days, or use news_trading: on.")
        for _key, label, cat in self.news.items():
            cat.validate(label)

    # ------------------------------------------------------------------
    # Session window, used to prove enabled sessions cannot collide
    # ------------------------------------------------------------------
    def window(self) -> tuple:
        """(start_second, duration_seconds) of this session's active window.

        The window opens at Range Start and closes at Stop Time — the same span
        `_session_trade_until()` uses at runtime. With Stop Time disabled the
        session runs until its own next Range Start, i.e. a full 24 hours, which
        is why a disabled Stop Time cannot coexist with another session.
        """
        from .timeutils import parse_hhmm
        start, _ = parse_hhmm(self.range_start)
        stop, stop_disabled = parse_hhmm(self.stop_time)
        if stop_disabled:
            return start, SECONDS_PER_DAY
        span = (stop - start) % SECONDS_PER_DAY
        return start, (span or SECONDS_PER_DAY)

    def overlaps(self, other: "StrategyConfig") -> bool:
        """Do these two sessions share any instant of the 24-hour clock?

        Both windows may wrap midnight (Asia opens 19:00 and stops 02:55), so
        the comparison is done on a circle: each window is unrolled twice and
        tested against the other over a 48-hour span.
        """
        a0, alen = self.window()
        b0, blen = other.window()
        for shift in (-SECONDS_PER_DAY, 0, SECONDS_PER_DAY):
            if a0 < (b0 + shift) + blen and (b0 + shift) < a0 + alen:
                return True
        return False


# ==========================================================================
# Instrument / symbol specification  (SymbolInfo* equivalents)
# ==========================================================================
@dataclass
class SymbolSpec:
    """Contract details used for price normalisation and P&L maths.

    In live mode these are read from MT5 and override whatever is set here.
    """
    name: str = "ES"                      # display name / MT5 symbol
    digits: int = 2                       # SYMBOL_DIGITS
    point: float = 0.01                   # SYMBOL_POINT
    tick_size: float = 0.25               # smallest price increment
    stops_level_points: int = 0           # SYMBOL_TRADE_STOPS_LEVEL (0 = none)
    volume_min: float = 1.0               # SYMBOL_VOLUME_MIN
    volume_max: float = 100.0             # SYMBOL_VOLUME_MAX
    volume_step: float = 1.0              # SYMBOL_VOLUME_STEP
    # money gained per 1.0 of price movement per 1.0 lot
    # ES = 50, NQ = 20, GC = 100, EURUSD 1 lot = 100000
    value_per_price_unit: float = 50.0
    currency: str = "USD"


# ==========================================================================
# Databento
# ==========================================================================
@dataclass
class DatabentoConfig:
    api_key: Optional[str] = None         # falls back to $DATABENTO_API_KEY
    dataset: str = "GLBX.MDP3"            # any Databento dataset
    symbols: str = "ES.c.0"               # any symbol
    stype_in: str = "continuous"          # raw_symbol | continuous | parent | instrument_id
    schema: str = "ohlcv-1m"              # base bar resolution used by the engine
    # --- contract selection inside parent/multi-instrument files ---
    # front_month_volume : per trading day, the highest-volume outright
    #                      contract; the roll only ever moves forward
    # symbol             : one fixed contract, e.g. "GCG5"
    # all                : no selection (single-instrument files only)
    contract_mode: str = "front_month_volume"
    contract_symbol: Optional[str] = None
    include_spreads: bool = False         # calendar spreads (GCG5-GCJ5)
    roll_min_volume: float = 0.0          # ignore thin contracts when rolling
    # Hour (server time) at which the futures TRADING DAY begins, and therefore
    # the only instant at which the contract may change.  CME opens the next
    # trading day at 18:00 New York, so bars from 18:00 onwards already belong
    # to tomorrow's date and already carry the new contract's volume.
    # Rolling at server midnight instead would switch instrument in the middle
    # of any session that spans midnight — an evening ORB, for example — and
    # inject the whole calendar spread into the price series as a fake move.
    roll_boundary_hour: int = 18
    # historical download helper
    start: Optional[str] = None           # "2025-01-01"
    end: Optional[str] = None             # "2025-06-30"
    output_dir: str = "data"


# ==========================================================================
# Backtest
# ==========================================================================
@dataclass
class BacktestConfig:
    dbn_paths: Any = field(default_factory=list)   # file, list of files, or glob
    initial_balance: float = 100000.0
    spread_points: float = 0.0            # extra points paid on entry (ask-bid)
    slippage_points: float = 0.0          # applied against you on entry and exit
    commission_per_lot_per_side: float = 0.0
    # if both SL and TP are touched inside the same bar, assume the worse one
    pessimistic_intrabar: bool = True
    out_dir: str = "backtest_out"
    report_name: str = "orb_backtest_report"


# ==========================================================================
# MetaTrader 5 (live execution)
# ==========================================================================
@dataclass
class MT5Config:
    symbol: str = "ES"                    # symbol name inside the MT5 terminal
    login: Optional[int] = None
    password: Optional[str] = None
    server: Optional[str] = None
    terminal_path: Optional[str] = None
    deviation_points: int = 0             # trade.SetDeviationInPoints(0)
    dry_run: bool = False                 # log orders instead of sending them
    # The signal is computed on the Databento instrument (CME GC) and executed
    # on the MT5 symbol (spot XAUUSD). They move together but quote tens of
    # dollars apart, so a price LEVEL from the feed is meaningless as an order
    # level on the broker — only the distances transfer.
    #   true  -> SL/TP are placed as DISTANCES measured from the real fill
    #            price. Use this whenever the two symbols differ.
    #   false -> SL/TP are the feed's absolute levels. Correct only when the
    #            MT5 symbol IS the instrument the bars came from.
    translate_levels: bool = True


# ==========================================================================
# Root
# ==========================================================================
@dataclass
class AppConfig:
    # `strategy` is the BASE rule set. With no `sessions:` block it is the one
    # and only session, so every existing single-session config keeps working
    # untouched. With a `sessions:` block it becomes the defaults each session
    # inherits, so shared settings are written once and only the differences
    # appear per session.
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    # name -> that session's complete rule set, in declaration order
    sessions: Dict[str, StrategyConfig] = field(default_factory=dict)
    symbol: SymbolSpec = field(default_factory=SymbolSpec)
    databento: DatabentoConfig = field(default_factory=DatabentoConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    mt5: MT5Config = field(default_factory=MT5Config)

    # broker/server clock: the EA's "server time"
    server_utc_offset_hours: Optional[float] = 0.0
    server_timezone: Optional[str] = None     # e.g. "Europe/Athens"; overrides offset

    # ---------------------------------------------------------------
    def enabled_sessions(self) -> List[StrategyConfig]:
        """Every session that is switched ON, in declaration order.

        A disabled session is dropped here and never reaches the engine, so it
        cannot build a range, cannot arm and cannot take a trade.
        """
        return [s for s in self.sessions.values() if s.enabled]

    def use_single_session(self, name: str = "MAIN") -> "StrategyConfig":
        """Collapse this config to ONE session driven by `strategy`.

        Tools that generate their own windows — the permutation matrix, the
        range sweep, the source verifier — build their settings on
        `cfg.strategy` and then run a backtest. If the config file also has a
        `sessions:` block, those settings would go nowhere: the backtest runs
        `enabled_sessions()`, so the tool's own permutation would be silently
        replaced by whatever the file happened to declare.

        Calling this makes the intent explicit and the wiring correct: the
        returned object IS the one session that will run, so mutating it is
        guaranteed to take effect. Always call it before mutating
        `cfg.strategy` in a tool.
        """
        self.strategy.name = name
        self.strategy.enabled = True
        self.sessions = {name: self.strategy}
        return self.strategy

    def validate_sessions(self) -> None:
        """Prove the enabled sessions cannot interfere with each other.

        The whole design rests on sessions never overlapping: one broker, one
        position at a time, each session flat at its own Stop Time before the
        next one opens. If two windows did overlap, one session's trade would
        block the other's entry and the results would silently depend on which
        session happened to fire first. So an overlap is a hard error, not a
        warning.
        """
        active = self.enabled_sessions()
        if not active:
            raise ValueError(
                "Every session is disabled — there is nothing to trade. "
                "Set enabled: true on at least one session.")
        for s in active:
            s.validate()
        if len(active) == 1:
            return
        # more than one session: each must hand over cleanly to the next
        for s in active:
            from .timeutils import parse_hhmm
            _, stop_disabled = parse_hhmm(s.stop_time)
            if stop_disabled:
                raise ValueError(
                    f"Session '{s.name}' has no Stop Time, so it runs for a full "
                    f"24 hours and would overlap every other session. Give it a "
                    f"stop_time, or leave only one session enabled.")
            if not s.close_at_stop_time:
                raise ValueError(
                    f"Session '{s.name}' has close_at_stop_time: false, so a "
                    f"trade can outlive its own window and block the next "
                    f"session. Set close_at_stop_time: true when running more "
                    f"than one session.")
        seen = {}
        for s in active:
            if s.magic in seen:
                raise ValueError(
                    f"Sessions '{seen[s.magic]}' and '{s.name}' share magic "
                    f"{s.magic}. Each session needs its own magic number, or "
                    f"MetaTrader cannot tell their positions apart. In YAML, "
                    f"drop the explicit `magic:` from one of them and it is "
                    f"assigned automatically; when building a config in code, "
                    f"set a different magic on each session.")
            seen[s.magic] = s.name

        for i, a in enumerate(active):
            for b in active[i + 1:]:
                if a.overlaps(b):
                    raise ValueError(
                        f"Sessions '{a.name}' ({a.range_start}-{a.stop_time}) and "
                        f"'{b.name}' ({b.range_start}-{b.stop_time}) overlap. "
                        f"Enabled sessions must not share any part of the clock — "
                        f"each one has to be flat before the next opens. Adjust a "
                        f"stop_time or a range_start, or disable one of them.")

    # ---------------------------------------------------------------
    @staticmethod
    def load(path: str) -> "AppConfig":
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        if path.lower().endswith((".yaml", ".yml")):
            if yaml is None:
                raise RuntimeError("PyYAML is not installed — use a .json config "
                                   "or `pip install pyyaml`")
            raw = yaml.safe_load(text) or {}
        else:
            raw = json.loads(text)
        return AppConfig.from_dict(raw)

    @staticmethod
    def from_dict(raw: Dict[str, Any]) -> "AppConfig":
        raw = dict(raw)

        # `defaults:` is the clearer name for the shared trade rules; `strategy:`
        # is the original name and still works. Only one of them may be present.
        if "defaults" in raw and "strategy" in raw:
            raise ValueError(
                "Use either `defaults:` or `strategy:` for the shared trade "
                "rules, not both — they are the same block under two names.")
        if "defaults" in raw:
            raw["strategy"] = raw.pop("defaults")

        # News may be written once at the top level instead of buried inside the
        # trade rules. A session can still override it. Top level is the default
        # for every session; anything set inside `defaults:` wins over it.
        base_block = dict(raw.get("strategy") or {})
        for key in ("news", "news_days", "news_trading"):
            if key in raw and key not in base_block:
                base_block[key] = raw[key]
        if base_block:
            raw["strategy"] = base_block

        def sub(cls, key, data=None):
            data = dict(raw.get(key) or {}) if data is None else dict(data)
            # nested per-category news block. A session that inherits the base
            # block already holds a built NewsConfig — pass it straight through.
            if key == "strategy" and isinstance(data.get("news"), NewsConfig):
                pass
            elif key == "strategy" and "news" in data:
                nd = data.pop("news") or {}
                if not isinstance(nd, dict):
                    raise ValueError("strategy.news must be a mapping of "
                                     "category -> {mode, dates}")
                unknown = set(nd) - set(NEWS_CATEGORIES)
                if unknown:
                    raise ValueError(
                        f"Unknown news categor(y/ies): {sorted(unknown)}. "
                        f"Valid keys: {sorted(NEWS_CATEGORIES)}")
                cats = {}
                for ck, cv in nd.items():
                    cv = cv or {}
                    if not isinstance(cv, dict):
                        raise ValueError(f"strategy.news.{ck} must have "
                                         f"'mode' and/or 'dates'")
                    bad = set(cv) - {"mode", "dates"}
                    if bad:
                        raise ValueError(f"strategy.news.{ck}: unknown key(s) "
                                         f"{sorted(bad)} (use mode / dates)")
                    cats[ck] = NewsCategory(**cv)
                data["news"] = NewsConfig(**cats)
            # `skip_dates` was renamed to `news_days`. Accept the old name so an
            # existing config keeps working, but say so — silently ignoring it
            # would mean trading on days the user meant to sit out.
            if key == "strategy" and "skip_dates" in data:
                old = data.pop("skip_dates")
                if "news_days" not in data:
                    data["news_days"] = old
                print("[RBEA] WARN  | config uses 'skip_dates', which is now "
                      "'news_days'. Using it as news_days with "
                      "news_trading='off' (same behaviour). Please rename it.")
            valid = {f for f in cls.__dataclass_fields__}
            unknown = set(data) - valid
            if unknown:
                raise ValueError(f"Unknown option(s) in '{key}': {sorted(unknown)}")
            return cls(**data)

        cfg = AppConfig(
            strategy=sub(StrategyConfig, "strategy"),
            symbol=sub(SymbolSpec, "symbol"),
            databento=sub(DatabentoConfig, "databento"),
            backtest=sub(BacktestConfig, "backtest"),
            mt5=sub(MT5Config, "mt5"),
            server_utc_offset_hours=raw.get("server_utc_offset_hours", 0.0),
            server_timezone=raw.get("server_timezone"),
        )
        if not cfg.databento.api_key:
            cfg.databento.api_key = os.environ.get("DATABENTO_API_KEY")

        # ---- sessions ------------------------------------------------
        raw_sessions = raw.get("sessions")
        if not raw_sessions:
            # No sessions block: the config describes exactly one session, which
            # is the `strategy` block itself. Nothing about the old behaviour
            # changes — this is still a single strategy on a single window.
            cfg.strategy.validate()
            if not cfg.strategy.name:
                cfg.strategy.name = "MAIN"
            cfg.sessions = {cfg.strategy.name: cfg.strategy}
        else:
            if not isinstance(raw_sessions, dict):
                raise ValueError(
                    "`sessions` must be a mapping of name -> settings, e.g.\n"
                    "  sessions:\n    asia:\n      enabled: true\n"
                    "      range_start: \"19:00\"")
            base = asdict(cfg.strategy)          # inherited defaults
            base.pop("news", None)               # NewsConfig is rebuilt below
            for sname, sdata in raw_sessions.items():
                sdata = dict(sdata or {})
                unknown = set(sdata) - set(StrategyConfig.__dataclass_fields__)
                if unknown:
                    raise ValueError(
                        f"Unknown option(s) in session '{sname}': "
                        f"{sorted(unknown)}")
                # A session's own window must be written in the session. If it
                # could be inherited, a missing range_start would silently fall
                # back to some unrelated default and the session would trade a
                # window nobody wrote down.
                required = ("range_start", "range_end", "stop_time")
                absent = [k for k in required if k not in sdata]
                if absent:
                    raise ValueError(
                        f"Session '{sname}' is missing {', '.join(absent)}. "
                        f"Every session must state its own range_start, "
                        f"range_end and stop_time — these are never inherited, "
                        f"so the window is always visible in the session.")
                merged = dict(base)
                # News is merged category by category, never replaced. A session
                # that names one category must not silently drop the dates of
                # the other seven — it states what differs, and inherits the
                # rest. Dates almost always stay shared (the calendar is the
                # calendar); it is usually only the mode that varies per session.
                import copy as _copy
                base_news = cfg.strategy.news
                override = sdata.pop("news", None)
                if override is None:
                    merged["news"] = _copy.deepcopy(base_news)
                else:
                    if not isinstance(override, dict):
                        raise ValueError(
                            f"session '{sname}': news must be a mapping of "
                            f"category -> {{mode, dates}}")
                    unknown_cat = set(override) - set(NEWS_CATEGORIES)
                    if unknown_cat:
                        raise ValueError(
                            f"session '{sname}': unknown news categor(y/ies) "
                            f"{sorted(unknown_cat)}. "
                            f"Valid keys: {sorted(NEWS_CATEGORIES)}")
                    cats = {}
                    for ckey, _clabel, basecat in base_news.items():
                        ov = override.get(ckey) or {}
                        if not isinstance(ov, dict):
                            raise ValueError(
                                f"session '{sname}': news.{ckey} must have "
                                f"'mode' and/or 'dates'")
                        bad = set(ov) - {"mode", "dates"}
                        if bad:
                            raise ValueError(
                                f"session '{sname}': news.{ckey} has unknown "
                                f"key(s) {sorted(bad)} (use mode / dates)")
                        cats[ckey] = NewsCategory(
                            mode=ov.get("mode", basecat.mode),
                            dates=ov.get("dates", basecat.dates))
                    merged["news"] = NewsConfig(**cats)
                # `engine_options` is merged OPTION BY OPTION, for the same
                # reason `news` is merged category by category: a session that
                # states one option must override that one option, not silently
                # discard the others.
                #
                #   defaults:  engine_options: {sl_range_mult: 0.5,
                #                               max_trades_per_session: 2}
                #   sessions:
                #     london:  engine_options: {sl_range_mult: 1.5}
                #
                # London wants a wider stop and everything else as before.
                # Replacing the whole dict would drop the cap back to the
                # engine's own default, and nothing would say so — the run
                # would simply take a different number of trades than the
                # config appears to ask for.
                # Options are inherited only when the session runs the SAME
                # engine as the defaults. A session that names a different
                # engine starts from an empty dict: the inherited options are
                # written in another strategy's vocabulary, and handing them
                # over would either be rejected as unknown or — worse — happen
                # to share a name and mean something else.
                session_engine = str(sdata.get("engine")
                                     or base.get("engine") or "orb").strip().lower()
                base_engine = str(base.get("engine") or "orb").strip().lower()
                session_options = sdata.pop("engine_options", None)
                if session_options is not None:
                    if not isinstance(session_options, dict):
                        raise ValueError(
                            f"Session '{sname}': engine_options must be a "
                            f"mapping of option names to values (got "
                            f"{type(session_options).__name__}).")
                    inherited = (dict(base.get("engine_options") or {})
                                 if session_engine == base_engine else {})
                    inherited.update(session_options)
                    merged["engine_options"] = inherited
                elif session_engine != base_engine:
                    merged["engine_options"] = {}
                merged.update(sdata)
                merged["name"] = sdata.get("name") or str(sname)
                # Every session needs its OWN magic number so MetaTrader can
                # tell their positions apart — the magic is the only tag that
                # survives on the broker's side. Inheriting one magic from the
                # defaults would make all sessions indistinguishable in the
                # terminal, in the deal history and in any manual clean-up.
                # A session that names its own magic keeps it; the rest get
                # base+1, base+2 ... in declaration order, which is stable
                # across runs so a restart re-attaches to the same positions.
                if "magic" not in sdata:
                    merged["magic"] = int(cfg.strategy.magic) + len(cfg.sessions) + 1
                cfg.sessions[str(sname)] = sub(StrategyConfig, "strategy", merged)

        cfg.validate_sessions()
        return cfg

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def journal_settings(cfg):
    """Level, file and time-stamp for the ONE journal a run writes.

    There is a single log file but many sessions, and a session may set its own
    `log_level`. Taking the level from the shared defaults would silently
    discard that: a session asking for `verbose` would get `normal`, and the
    detail it was turned on for would never be written.

    So the level is the MOST verbose any enabled session asks for. Turning
    detail on for one session turns it on, and a session set to `none` cannot
    silence a sibling that wants it. File path and time-stamp come from the
    first session that names them, falling back to the defaults.
    """
    from .logger import parse_log_level
    sessions = list(cfg.enabled_sessions()) or [cfg.strategy]
    level = max(parse_log_level(s.log_level) for s in sessions)
    file_path = next((s.log_file for s in sessions if s.log_file),
                     cfg.strategy.log_file)
    show_time = next((s.log_show_time for s in sessions
                      if s.log_show_time is not None), cfg.strategy.log_show_time)
    return level, file_path, bool(show_time)
