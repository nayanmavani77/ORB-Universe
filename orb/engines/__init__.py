"""The engines.

Importing this package registers every built-in engine by name. `orb.registry`
imports it lazily the first time anything resolves an engine, so nothing in the
core has to depend on a particular strategy.

Adding an engine is four steps:

  1. create `orb/engines/<name>/` with the five contract files — see
     `orb/engines/base.py`
  2. call `register(...)` in its `__init__.py`
  3. import it below
  4. add it to `tests/test_multi_engine.py`'s contract check, which walks this
     list and fails if a file is missing

Nothing else in the system needs to change: config, backtest, live, sweeps and
reports all reach an engine through the registry.
"""
from . import orb, orb_reverse  # noqa: F401  (imported for registration)
from .base import EngineSettings, settings_of  # noqa: F401

#: every built-in engine package, in registration order
BUILTIN = (orb, orb_reverse)

__all__ = ["BUILTIN", "EngineSettings", "settings_of", "orb", "orb_reverse"]
