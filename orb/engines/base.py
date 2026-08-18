"""The contract every engine follows.

An engine is a folder under `orb/engines/` containing the same five files:

    __init__.py   calls `register(...)` so the engine exists by name
    strategy.py   the strategy class
    settings.py   an `EngineSettings` subclass — the engine's own options
    grid.py       the sweep axes for this engine
    config.yaml   the COMPLETE master configuration for this engine

Those five and nothing else — `tests/test_multi_engine.py` fails an engine
folder that grows a sixth file, so the shape stays uniform. Prose belongs in
`docs/<engine>/README.md`, not in the package.

Two rules keep engines interchangeable:

1. **The strategy constructor signature is fixed.** `Engine` builds it as
   `cls(cfg, broker, store=..., logger=...)`. Anything else cannot be swapped
   in.

2. **An engine's options live in the session's `engine_options` dict**, and are
   read only through that engine's `EngineSettings` subclass. The core never
   learns a strategy's vocabulary — `orb/config.py` has no idea what
   `sl_range_mult` means, and does not need to.

Rule 2 replaces the previous scheme, where the reversal stamped undeclared
`rev_*` attributes onto `StrategyConfig`. Those were invisible to `asdict()`,
which meant they were silently dropped by session inheritance and absent from
`--show-config`. A plain dict field has neither problem.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields as dc_fields
from typing import Any, ClassVar, Dict, Mapping


@dataclass
class EngineSettings:
    """Base class for one engine's options.

    Subclasses declare plain dataclass fields with defaults, and override
    `validate()`. `from_options()` and `to_options()` handle the dict round
    trip, including aliases.

    `ALIASES` maps an old or alternative spelling to the canonical field name,
    so a config written before a rename keeps loading:

        ALIASES = {"max_trades": "max_trades_per_session"}
    """

    ALIASES: ClassVar[Dict[str, str]] = {}

    # ------------------------------------------------------------------
    @classmethod
    def field_names(cls) -> set:
        return {f.name for f in dc_fields(cls)}

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> "EngineSettings":
        """Build from a session's `engine_options`.

        An unrecognised key is an error rather than being ignored: a typo in
        `sl_range_mult` that silently did nothing would be far worse than a
        loud failure, because the backtest would run and quietly use defaults.
        """
        from ..config import StrategyConfig
        core_fields = set(StrategyConfig.__dataclass_fields__)

        data: Dict[str, Any] = {}
        valid = cls.field_names()
        unknown, misplaced = [], []
        for key, value in dict(options or {}).items():
            name = cls.ALIASES.get(str(key), str(key))
            if name in valid:
                data[name] = value
            elif str(key) in core_fields or name in core_fields:
                misplaced.append(str(key))
            else:
                unknown.append(str(key))
        if misplaced:
            # The failure this prevents is silent and expensive: a core field
            # written under engine_options is read by nobody. The core enforces
            # it from the SESSION, so `max_trades_per_session: 3` in the wrong
            # block means no cap at all — the run quietly takes more trades
            # than the config appears to ask for.
            raise ValueError(
                f"{sorted(misplaced)} are session settings, not engine options. "
                f"Put them directly on the session, not under engine_options:\n"
                f"    sessions:\n      london:\n        "
                + "\n        ".join(f"{m}: ..." for m in sorted(misplaced))
                + "\n        engine_options:\n          ...")
        if unknown:
            allowed = ", ".join(sorted(valid | set(cls.ALIASES)))
            raise ValueError(
                f"Unknown engine_options for {cls.__name__}: "
                f"{sorted(unknown)}. Valid options: {allowed}.")
        obj = cls(**data)
        obj.validate()
        return obj

    def to_options(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in dc_fields(self)}

    # ------------------------------------------------------------------
    def validate(self) -> None:
        """Raise ValueError with a message a user can act on."""

    def describe(self) -> str:
        """One line for the journal banner."""
        return ", ".join(f"{k}={v!r}" for k, v in self.to_options().items())


@dataclass
class GridItem:
    """One point on an engine's sweep grid.

    Every engine's `grid.build()` returns a list of these — same type, same
    field names, whatever the engine. A sweep tool can therefore treat all
    engines alike: `item.run_name` and `item.cfg` always mean the same thing,
    and `item.row()` always produces the flat record for the results CSV.

    `axes` holds whatever varies for THAT engine (`sl_mode` for orb,
    `sl_range_mult` / `direction` for orb_reverse), flattened into the row so
    each engine's summary tables come out with its own columns.
    """
    run_name: str
    cfg: Any
    engine: str
    session: str
    signal_timeframe: str
    orb_minutes: int
    news_mode: str
    risk_reward: float
    range_start: str
    range_end: str
    stop_time: str
    settings: Any = None
    axes: Dict[str, Any] = field(default_factory=dict)

    def row(self) -> Dict[str, Any]:
        """The flat record written to the results CSV — everything except the
        two objects, with this engine's own axes flattened in."""
        out = {k: v for k, v in self.__dict__.items()
               if k not in ("cfg", "settings", "axes")}
        out.update(self.axes)
        return out


def settings_of(cfg, settings_cls):
    """Read a session's `engine_options` through its engine's settings class.

    The single place a strategy should get its options from, so the defaults
    live in exactly one file instead of being repeated at each `getattr` call
    site.
    """
    return settings_cls.from_options(getattr(cfg, "engine_options", None) or {})
