"""Which strategy class a session runs.

Before this existed there was exactly one strategy, named directly inside
`Engine.__init__`, and a second strategy could only be reached by rebinding
that module global — a process-wide monkey-patch. Three separate copies of that
patch existed, and because it was global, two different engines could never run
in the same process. That is what this module removes.

An engine registers itself once:

    register("breakout", OrbStrategy,
             description="Trade the opening-range breakout.")

and a session names it in config:

    sessions:
      london:
        engine: reversal

`Engine` then asks `resolve(cfg.engine)` for the class to build. Since the
lookup happens per session, Asia can run one engine while London runs another,
in the same process, in both backtest and live.

Nothing in `orb/` outside this file imports `orb.engines` at module scope. The
registry bootstraps itself on first use instead, so importing `orb.engine`
never drags in every strategy, and a strategy is free to import from the core.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Type

BUILTIN_PACKAGE = "orb.engines"

_ENGINES: Dict[str, "EngineSpec"] = {}
_BOOTSTRAPPED = False


@dataclass(frozen=True)
class EngineSpec:
    """One registered engine.

    name          the value written in `engine:` in the config
    strategy_cls  built as `cls(cfg, broker, store=..., logger=...)`
    settings_cls  reads and validates the session's `engine_options` dict;
                  may be None for an engine that takes no options
    description   one line, shown when an unknown engine is requested
    """
    name: str
    strategy_cls: Type
    settings_cls: Optional[Type] = None
    description: str = ""


# --------------------------------------------------------------------------
def register(name: str, strategy_cls: Type,
             settings_cls: Optional[Type] = None,
             description: str = "", replace: bool = False) -> EngineSpec:
    """Add an engine. Re-registering the same name is refused unless `replace`,
    because a silent overwrite would mean a config's `engine:` quietly changed
    meaning depending on import order."""
    key = _norm(name)
    if not key:
        raise ValueError("An engine name cannot be empty.")
    if key in _ENGINES and not replace:
        existing = _ENGINES[key].strategy_cls
        if existing is strategy_cls:
            return _ENGINES[key]
        raise ValueError(
            f"Engine '{key}' is already registered to {existing.__module__}."
            f"{existing.__qualname__}. Pass replace=True to override it.")
    spec_obj = EngineSpec(name=key, strategy_cls=strategy_cls,
                          settings_cls=settings_cls, description=description)
    _ENGINES[key] = spec_obj
    return spec_obj


def resolve(name: str) -> Type:
    """The strategy class for `name`."""
    return spec(name).strategy_cls


def spec(name: str) -> EngineSpec:
    key = _norm(name)
    _bootstrap()
    if key not in _ENGINES:
        known = ", ".join(sorted(_ENGINES)) or "(none registered)"
        raise ValueError(
            f"Unknown engine '{name}'. Available engines: {known}. "
            f"Check the 'engine:' value for this session in your config.")
    return _ENGINES[key]


def settings_for(name: str, options: Optional[Dict[str, Any]] = None):
    """Build and validate an engine's settings from a session's
    `engine_options`. Returns None for an engine that declares no settings
    class — but a non-empty options dict is then an error, because it means the
    user wrote settings that nothing will ever read."""
    s = spec(name)
    opts = dict(options or {})
    if s.settings_cls is None:
        if opts:
            raise ValueError(
                f"Engine '{s.name}' takes no engine_options, but "
                f"{sorted(opts)} were given.")
        return None
    return s.settings_cls.from_options(opts)


def names() -> List[str]:
    _bootstrap()
    return sorted(_ENGINES)


def specs() -> List[EngineSpec]:
    _bootstrap()
    return [_ENGINES[k] for k in sorted(_ENGINES)]


def is_registered(name: str) -> bool:
    _bootstrap()
    return _norm(name) in _ENGINES


def clear() -> None:
    """Testing only — empty the registry and allow a fresh bootstrap."""
    global _BOOTSTRAPPED
    _ENGINES.clear()
    _BOOTSTRAPPED = False


# --------------------------------------------------------------------------
def _norm(name: str) -> str:
    return str(name or "").strip().lower().replace("-", "_")


def _bootstrap() -> None:
    """Import the built-in engines the first time anything asks.

    Doing it lazily rather than at import time keeps `orb.engine` free of any
    dependency on a particular strategy, so a strategy can import the core
    without a cycle. It also means a test that builds an `Engine` directly does
    not have to remember to import the engine package first.
    """
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    _BOOTSTRAPPED = True          # set first: a failed import must not retry
    try:
        __import__(BUILTIN_PACKAGE)
    except ImportError as exc:    # pragma: no cover - only on a broken install
        raise ImportError(
            f"Could not import the built-in engines from '{BUILTIN_PACKAGE}': "
            f"{exc}") from exc
