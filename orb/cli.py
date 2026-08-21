"""One command-line spec, shared by every entry point.

`SPEC` below lists one `Opt` per configuration field. It is the single place
where a setting's flag name, type and help text are defined, and it drives
three things at once:

    * the argparse parser          (`build_parser`)
    * applying the values          (`apply_options`)
    * the generated documentation  (`tools/gen_cli_docs.py` -> docs/CLI.md)

`tests/test_cli.py` asserts that SPEC covers **every** field of `AppConfig`,
so a new config option cannot be added without also getting a flag and a
documentation entry.
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, fields as dc_fields, is_dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional, Sequence, Tuple

from .config import AppConfig, NEWS_CATEGORIES

#: the default configuration — the orb engine's own master config. There is no
#: parent config file any more; each engine's config.yaml is the complete
#: configuration for that engine.
ENGINE_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "engines", "orb", "config.yaml")


# ==========================================================================
@dataclass
class Opt:
    flags: Tuple[str, ...]      # ("--risk-reward", "--rr")
    path: str                   # "strategy.risk_reward"
    kind: str                   # str | int | float | bool | choice | list
    help: str
    group: str = "Strategy"
    choices: Optional[Tuple[str, ...]] = None
    metavar: Optional[str] = None

    @property
    def dest(self) -> str:
        return "opt_" + self.path.replace(".", "__")


G_SESSION = "Session (broker/server time)"
G_STRAT = "Strategy"
G_SYMBOL = "Instrument / contract specification"
G_DATA = "Data source (Databento)"
G_COST = "Costs and account"
G_OUT = "Output and logging"
G_LIVE = "Live trading (MetaTrader 5)"
G_NEWS = "News categories"


SPEC: List[Opt] = [
    # ---------------- session ----------------
    Opt(("--instrument",), "strategy.instrument", "str",
        "Which instrument this session trades — a key in the `instruments:` "
        "block, e.g. gc or es. Blank means the run's single instrument.",
        G_SESSION, metavar="NAME"),
    Opt(("--engine",), "strategy.engine", "str",
        "Which strategy engine this session runs — a name registered in "
        "orb/engines/, e.g. orb or orb_reverse. Each session may use a "
        "different one; they run side by side.", G_SESSION, metavar="NAME"),
    Opt(("--range-start",), "strategy.range_start", "str",
        "Range window start, HH:MM.", G_SESSION, metavar="HH:MM"),
    Opt(("--range-end",), "strategy.range_end", "str",
        "Range window end, HH:MM.", G_SESSION, metavar="HH:MM"),
    Opt(("--stop-time",), "strategy.stop_time", "str",
        'Stop trading at HH:MM. "0" disables it and the session runs until the '
        "next range starts.", G_SESSION, metavar="HH:MM"),
    Opt(("--utc-offset",), "server_utc_offset_hours", "float",
        "Broker server offset from UTC in hours, e.g. 2 or -5. Databento is "
        "UTC; this is what makes 09:00 here mean 09:00 on your MT5 chart.",
        G_SESSION, metavar="HOURS"),
    Opt(("--tz",), "server_timezone", "str",
        'DST-aware server zone, e.g. "America/New_York". Overrides '
        "--utc-offset when set.", G_SESSION, metavar="ZONE"),

    # ---------------- strategy ----------------
    Opt(("--signal-timeframe", "--tf"), "strategy.signal_timeframe", "str",
        "Timeframe for the range and the breakout closes: M1 M5 M15 M30 H1 H4 D1.",
        G_STRAT, metavar="TF"),
    Opt(("--sl-mode",), "strategy.sl_mode", "choice",
        "Stop loss placement: the range midpoint, or the opposite side of the "
        "range.", G_STRAT, choices=("mid_range", "full_range")),
    Opt(("--risk-reward", "--rr"), "strategy.risk_reward", "float",
        "Take profit as a multiple of the stop distance, measured from the "
        "real fill price.", G_STRAT, metavar="R"),
    Opt(("--lots",), "strategy.lots", "float", "Position size in lots.", G_STRAT),
    Opt(("--require-range-reentry", "--reentry"), "strategy.require_range_reentry",
        "bool", "After a trade closes, require a close back inside the range "
        "before the next breakout is taken. Negate with --no-reentry.", G_STRAT),
    Opt(("--breakeven", "--break-even"), "strategy.breakeven", "bool",
        "Move the stop loss to the entry price once the trade is far enough "
        "in front — see --breakeven-trigger for how far. From there only a gap "
        "or slippage through the entry loses. Negate with --no-breakeven.",
        G_STRAT),
    Opt(("--breakeven-trigger",), "strategy.breakeven_trigger_r", "float",
        "How far in front the trade must be before the stop moves to entry, "
        "as a multiple of its own risk. 1.0 = 1:1, the trade is up by what it "
        "was risking. Only used when --breakeven is on.", G_STRAT, metavar="R"),
    Opt(("--pullback-entry", "--pullback"), "strategy.pullback_entry", "bool",
        "Enter on a PULLBACK to the broken level instead of on the breakout "
        "close: long when price comes back and touches the range high, short "
        "when it touches the range low. A touch is enough — the bar need not "
        "close there, and the entry fires during the running bar. Negate with "
        "--no-pullback.", G_STRAT),
    Opt(("--max-trades-per-session", "--max-trades"),
        "strategy.max_trades_per_session", "int",
        "Cap on trades per session; 0 means unlimited.", G_STRAT, metavar="N"),
    Opt(("--close-at-stop-time", "--close-at-stop"), "strategy.close_at_stop_time",
        "bool", "Flatten any open position at the stop time. Negate with "
        "--no-close-at-stop.", G_STRAT),
    Opt(("--news-days",), "strategy.news_days", "str",
        "News Days. Single dates and ranges, comma separated: "
        "2025.12.25,2026.01.01,2026.04.03-2026.04.06", G_NEWS, metavar="LIST"),
    Opt(("--news-trading",), "strategy.news_trading", "choice",
        "How the News Days list is applied. on = trade every day, News Days "
        "included (the list is ignored). off = never trade on a News Day. "
        "only = trade on News Days and on no other day.", G_NEWS,
        choices=("on", "off", "only")),
    Opt(("--magic",), "strategy.magic", "int",
        "Magic number identifying this EA's orders.", G_STRAT),
    Opt(("--comment",), "strategy.comment", "str", "Order comment.", G_STRAT),

    # ---------------- instrument ----------------
    Opt(("--symbol",), "symbol.name", "str",
        "Instrument name shown in the report, e.g. GC.", G_SYMBOL),
    Opt(("--digits",), "symbol.digits", "int",
        "Price decimals used for rounding SL and TP.", G_SYMBOL),
    Opt(("--point",), "symbol.point", "float",
        "Point size; the broker stop level is quoted in these.", G_SYMBOL),
    Opt(("--tick-size",), "symbol.tick_size", "float",
        "Minimum price increment, e.g. 0.10 for gold.", G_SYMBOL),
    Opt(("--stops-level",), "symbol.stops_level_points", "int",
        "Broker minimum SL/TP distance in points; 0 means no restriction.",
        G_SYMBOL, metavar="POINTS"),
    Opt(("--volume-min",), "symbol.volume_min", "float",
        "Smallest tradeable volume.", G_SYMBOL),
    Opt(("--volume-max",), "symbol.volume_max", "float",
        "Largest tradeable volume.", G_SYMBOL),
    Opt(("--volume-step",), "symbol.volume_step", "float",
        "Volume increment used when normalising the lot size.", G_SYMBOL),
    Opt(("--value-per-point",), "symbol.value_per_price_unit", "float",
        "Money per 1.0 of price movement per 1 lot. GC 100, ES 50, NQ 20, "
        "CL 1000, EURUSD 100000.", G_SYMBOL, metavar="MONEY"),
    Opt(("--currency",), "symbol.currency", "str",
        "Account currency label used in the report.", G_SYMBOL),

    # ---------------- data ----------------
    Opt(("--data", "-d"), "backtest.dbn_paths", "list",
        "DBN file(s), a directory or a glob. Overrides the config.", G_DATA,
        metavar="PATH"),
    Opt(("--contract-mode",), "databento.contract_mode", "choice",
        "How to pick an instrument inside a multi-contract file.", G_DATA,
        choices=("front_month_volume", "symbol", "all")),
    Opt(("--contract",), "databento.contract_symbol", "str",
        "Fixed contract, e.g. GCZ5. Implies --contract-mode symbol.", G_DATA,
        metavar="SYM"),
    Opt(("--include-spreads",), "databento.include_spreads", "bool",
        "Keep calendar spreads such as GCG5-GCJ5. Off by default, and you "
        "almost never want them on.", G_DATA),
    Opt(("--news-mode",), "strategy.news_mode", "choice",
        "One switch for the whole news filter: every category takes this mode. "
        "on = trade news days, off = skip them, only = trade nothing else. "
        "Per session: --set sessions.asia.news_mode=off", G_NEWS,
        choices=("on", "off", "only")),
    Opt(("--roll-min-volume",), "databento.roll_min_volume", "float",
        "Ignore contracts below this daily volume when choosing the front "
        "month.", G_DATA, metavar="VOL"),
    Opt(("--roll-boundary-hour",), "databento.roll_boundary_hour", "int",
        "Server-time hour at which the futures trading day starts, and the "
        "only instant the contract may change. 18 = the CME 18:00 New York "
        "open (default). Use 0 for a plain midnight boundary.", G_DATA,
        metavar="HOUR"),
    Opt(("--dataset",), "databento.dataset", "str",
        "Databento dataset, e.g. GLBX.MDP3.", G_DATA),
    Opt(("--db-symbols",), "databento.symbols", "str",
        "Databento symbol request, e.g. GC.FUT or ES.c.0.", G_DATA,
        metavar="SYMBOLS"),
    Opt(("--stype-in",), "databento.stype_in", "choice",
        "Databento symbology type of the request.", G_DATA,
        choices=("raw_symbol", "continuous", "parent", "instrument_id")),
    Opt(("--schema",), "databento.schema", "str",
        "Databento schema; must be an OHLCV one, e.g. ohlcv-1m.", G_DATA),
    Opt(("--db-api-key",), "databento.api_key", "str",
        "Databento API key. Defaults to $DATABENTO_API_KEY.", G_DATA,
        metavar="KEY"),
    Opt(("--download-start",), "databento.start", "str",
        "Start date for download_data.py.", G_DATA, metavar="DATE"),
    Opt(("--download-end",), "databento.end", "str",
        "End date for download_data.py.", G_DATA, metavar="DATE"),
    Opt(("--download-dir",), "databento.output_dir", "str",
        "Where download_data.py writes files.", G_DATA, metavar="DIR"),

    # ---------------- costs ----------------
    Opt(("--balance",), "backtest.initial_balance", "float",
        "Starting account balance.", G_COST, metavar="MONEY"),
    Opt(("--spread",), "backtest.spread_points", "float",
        "Bid/ask spread in points, charged on entry.", G_COST, metavar="POINTS"),
    Opt(("--slippage",), "backtest.slippage_points", "float",
        "Slippage in points, always applied against you.", G_COST,
        metavar="POINTS"),
    Opt(("--commission",), "backtest.commission_per_lot_per_side", "float",
        "Commission per lot per side; charged twice per round turn.", G_COST,
        metavar="MONEY"),
    Opt(("--pessimistic-intrabar",), "backtest.pessimistic_intrabar", "bool",
        "When SL and TP both sit inside one bar, assume the stop was hit "
        "first. Negate with --no-pessimistic-intrabar.", G_COST),

    # ---------------- output ----------------
    Opt(("--out",), "backtest.out_dir", "str",
        "Directory for the report and CSV files.", G_OUT, metavar="DIR"),
    Opt(("--name",), "backtest.report_name", "str",
        "Base name for the output files, so parallel runs do not overwrite "
        "each other.", G_OUT),
    Opt(("--log-level",), "strategy.log_level", "choice",
        "Journal detail: errors only, normal, or verbose.", G_OUT,
        choices=("none", "normal", "verbose")),
    Opt(("--log-file",), "strategy.log_file", "str",
        "Also append the journal to this file.", G_OUT, metavar="PATH"),
    Opt(("--log-show-time",), "strategy.log_show_time", "bool",
        "Prefix journal lines with the server time. Negate with "
        "--no-log-show-time.", G_OUT),

    # ---------------- live ----------------
    Opt(("--mt5-symbol",), "mt5.symbol", "str",
        "Symbol name inside the MT5 terminal.", G_LIVE, metavar="SYM"),
    Opt(("--mt5-login",), "mt5.login", "int", "MT5 account number.", G_LIVE),
    Opt(("--mt5-password",), "mt5.password", "str", "MT5 password.", G_LIVE),
    Opt(("--mt5-server",), "mt5.server", "str", "MT5 broker server name.", G_LIVE),
    Opt(("--mt5-path",), "mt5.terminal_path", "str",
        "Path to terminal64.exe, if it is not the default install.", G_LIVE,
        metavar="PATH"),
    Opt(("--deviation",), "mt5.deviation_points", "int",
        "Maximum price deviation in points when sending an order.", G_LIVE,
        metavar="POINTS"),
    Opt(("--translate-levels",), "mt5.translate_levels", "bool",
        "Carry SL/TP across as DISTANCES from the real fill, for when the data "
        "feed and the MT5 symbol are different instruments (CME GC signal, "
        "spot XAUUSD execution). Turn it off only if the MT5 symbol IS the "
        "instrument the bars came from.", G_LIVE),
    Opt(("--dry-run",), "mt5.dry_run", "bool",
        "Log orders instead of sending them to MT5.", G_LIVE),
]

# Two flags per category, generated from the same table the config uses, so a
# category can never exist without a way to set it from the command line.
for _key, (_label, _flag) in NEWS_CATEGORIES.items():
    SPEC.append(Opt((f"--{_flag}-dates",), f"strategy.news.{_key}.dates", "str",
                    f"{_label}: the release dates. Single dates and from-to "
                    f"ranges, comma or newline separated.", G_NEWS,
                    metavar="LIST"))
    SPEC.append(Opt((f"--{_flag}-mode",), f"strategy.news.{_key}.mode", "choice",
                    f"{_label}: how its dates are used. on = no restriction, "
                    f"off = never trade them, only = trade them and nothing "
                    f"else.", G_NEWS, choices=("on", "off", "only")))

GROUP_ORDER = [G_SESSION, G_STRAT, G_NEWS, G_SYMBOL, G_DATA, G_COST, G_OUT, G_LIVE]


# ==========================================================================
# Fields that deliberately have no dedicated --flag:
#   sessions           a mapping, not a leaf — driven by --sessions and
#                      --set sessions.<name>.<field>=<value>
#   strategy.name      per-session identity, set by the session's key in YAML
#   strategy.enabled   per-session ON/OFF, driven by --sessions
#   strategy.engine_options
#                      a mapping whose keys belong to whichever engine the
#                      session runs, so there is no fixed set of flags to
#                      generate. Driven by
#                      --set sessions.<name>.engine_options.<option>=<value>
#   instruments        a mapping of name -> {signal, mt5, value_per_point,
#                      data}. Like `sessions`, its shape is defined by the
#                      user, so there is no fixed set of flags to generate.
#                      Edit the engine's config.yaml, or use
#                      --set instruments.<name>.<field>=<value>
NO_FLAG = {"sessions", "instruments", "strategy.name", "strategy.enabled",
           "strategy.engine_options"}


def config_paths(cls=AppConfig, prefix: str = "") -> List[str]:
    """Every leaf field of AppConfig that a --flag is expected to cover."""
    import dataclasses
    out: List[str] = []
    for f in dc_fields(cls):
        path = f"{prefix}{f.name}"
        if path in NO_FLAG:
            continue
        nested = None
        if f.default_factory is not dataclasses.MISSING:   # type: ignore[misc]
            try:
                candidate = f.default_factory()            # type: ignore[misc]
            except Exception:
                candidate = None
            if is_dataclass(candidate):
                nested = candidate
        if nested is not None:
            out.extend(config_paths(type(nested), path + "."))
        else:
            out.append(path)
    return out


def get_path(cfg, path: str):
    node = cfg
    for part in path.split("."):
        node = getattr(node, part)
    return node


def set_path(cfg, path: str, value) -> None:
    parts = path.split(".")
    node = cfg
    for part in parts[:-1]:
        node = getattr(node, part)
    setattr(node, parts[-1], value)


# ==========================================================================
def build_parser(prog: str, description: str, epilog: str = "",
                 include: Optional[Sequence[str]] = None) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=prog, description=description, epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", "-c", default=ENGINE_CONFIG, help="config file")

    groups = {}
    for opt in SPEC:
        if include and opt.group not in include:
            continue
        if opt.group not in groups:
            groups[opt.group] = p.add_argument_group(opt.group)
        g = groups[opt.group]
        kw: dict = {"dest": opt.dest, "default": None, "help": opt.help}
        if opt.kind == "bool":
            kw["action"] = argparse.BooleanOptionalAction
        elif opt.kind == "list":
            kw["nargs"] = "*"
            kw["metavar"] = opt.metavar or "PATH"
        else:
            kw["type"] = {"int": int, "float": float}.get(opt.kind, str)
            if opt.choices:
                kw["choices"] = list(opt.choices)
            else:
                kw["metavar"] = opt.metavar or opt.path.split(".")[-1].upper()
        g.add_argument(*opt.flags, **kw)
    return p


def apply_options(cfg: AppConfig, ns: argparse.Namespace) -> AppConfig:
    """Apply every --flag that was actually given. Does not validate."""
    # remember which strategy-scoped flags were used, so they can be pushed
    # into every session below (see _fan_out_to_sessions)
    touched = []
    for opt in SPEC:
        if not hasattr(ns, opt.dest):
            continue
        value = getattr(ns, opt.dest)
        if value is None:
            continue
        set_path(cfg, opt.path, value)
        if opt.path.startswith("strategy."):
            touched.append(opt.path.split(".", 1)[1])

    # --- conveniences that touch more than one field ---
    rng = getattr(ns, "range_window", None)
    if rng:
        if "-" not in rng:
            raise ValueError("--range expects HH:MM-HH:MM, e.g. 13:30-14:30")
        a, b = (x.strip() for x in rng.split("-", 1))
        cfg.strategy.range_start, cfg.strategy.range_end = a, b
        touched += ["range_start", "range_end"]
    if getattr(ns, "opt_databento__contract_symbol", None) and \
            getattr(ns, "opt_databento__contract_mode", None) is None:
        cfg.databento.contract_mode = "symbol"
    if getattr(ns, "opt_server_utc_offset_hours", None) is not None and \
            getattr(ns, "opt_server_timezone", None) is None:
        cfg.server_timezone = None
    if getattr(ns, "quiet", False):
        cfg.strategy.log_level = "none"
        touched.append("log_level")

    _fan_out_to_sessions(cfg, touched)
    _select_sessions(cfg, getattr(ns, "sessions", None))

    for spec in getattr(ns, "overrides", None) or []:
        apply_set(cfg, spec)
    return cfg


def _fan_out_to_sessions(cfg: AppConfig, touched) -> None:
    """Push strategy-scoped flags into every session.

    With no `sessions:` block the single session IS `cfg.strategy` (the same
    object), so this is a no-op and nothing about the old behaviour changes.
    With a sessions block, `cfg.strategy` is only the inherited base — a flag
    like `--rr 3` would otherwise be silently swallowed. A global flag means
    "for every session"; use `--set sessions.asia.risk_reward=3` to target one.
    """
    for session in cfg.sessions.values():
        if session is cfg.strategy:
            continue
        for field_name in touched:
            # A path, not a bare name: the per-category news flags are
            # `strategy.news.<category>.dates` / `.mode`, so what lands in
            # `touched` is `news.<category>.dates`. A plain setattr on that
            # raised AttributeError and took every `--<cat>-dates` flag with it
            # on any config that has a sessions block — which is both of them.
            set_path(session, field_name, get_path(cfg.strategy, field_name))


def _select_sessions(cfg: AppConfig, wanted: Optional[str]) -> None:
    """--sessions asia,new_york : enable exactly these, disable the rest."""
    if not wanted:
        return
    names = [n.strip() for n in str(wanted).replace(";", ",").split(",") if n.strip()]
    known = {k.lower(): k for k in cfg.sessions}
    chosen = set()
    for n in names:
        key = known.get(n.lower())
        if key is None:
            raise ValueError(
                f"--sessions: unknown session '{n}'. "
                f"Available: {', '.join(cfg.sessions) or 'none'}")
        chosen.add(key)
    for key, session in cfg.sessions.items():
        session.enabled = key in chosen


# --------------------------------------------------------------------------
def _coerce(current, text: str):
    if text.lower() in ("none", "null", "~"):
        return None
    if isinstance(current, bool) or text.lower() in ("true", "false", "yes", "no"):
        return text.lower() in ("true", "yes", "1")
    if isinstance(current, int) and not isinstance(current, bool):
        return int(text)
    if isinstance(current, float):
        return float(text)
    return text


def apply_set(cfg: AppConfig, spec: str) -> None:
    """--set path.to.field=value, for anything not worth a dedicated flag."""
    if "=" not in spec:
        raise ValueError(f"--set needs PATH=VALUE, got '{spec}'")
    path, value = spec.split("=", 1)
    path = path.strip()
    node = cfg
    parts = path.split(".")
    for part in parts[:-1]:
        # `sessions` is a mapping, so a path like sessions.asia.risk_reward has
        # to step through a dict key as naturally as through an attribute
        if isinstance(node, dict):
            if part not in node:
                raise ValueError(
                    f"--set: unknown session '{part}' in '{path}'. "
                    f"Available: {', '.join(node) or 'none'}")
            node = node[part]
            continue
        if not hasattr(node, part):
            raise ValueError(f"--set: unknown section '{part}' in '{path}'")
        node = getattr(node, part)
    leaf = parts[-1]
    if isinstance(node, dict):
        raise ValueError(f"--set: '{path}' stops on a mapping, not an option")
    if not hasattr(node, leaf):
        raise ValueError(f"--set: unknown option '{leaf}' in '{path}'")
    setattr(node, leaf, _coerce(getattr(node, leaf), value.strip()))


# --------------------------------------------------------------------------
_DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
               "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
               "%Y-%m-%d", "%Y.%m.%d %H:%M", "%Y.%m.%d", "%Y/%m/%d")


def parse_clamp(text: Optional[str]) -> Optional[Tuple[datetime, bool]]:
    """Parse a --start / --end value.

    Returns (utc_datetime, is_date_only).  A date-only --end is inclusive of
    that whole day; a value with a time is taken literally and is exclusive.
    """
    if not text:
        return None
    s = text.strip()
    for fmt in _DT_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        date_only = "%H" not in fmt
        return dt.replace(tzinfo=timezone.utc), date_only
    raise ValueError(
        f"Could not read date/time '{text}'. Use YYYY-MM-DD or "
        f"'YYYY-MM-DD HH:MM' (UTC).")
