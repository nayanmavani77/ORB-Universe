"""Options for the reversal engine.

These arrive in a session's `engine_options` dict:

    sessions:
      london:
        engine: reversal
        engine_options:
          sl_range_mult: 0.75
          direction: reverse
          max_trades_per_session: 3

Previously they were stamped onto `StrategyConfig` as undeclared `rev_*`
attributes. That had two silent failures: `asdict()` cannot see a non-field
attribute, so the options were dropped by session inheritance and absent from
`--show-config`. A declared dict field has neither problem.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Dict

from ..base import EngineSettings

FORWARD = "forward"
REVERSE = "reverse"
DIRECTIONS = (FORWARD, REVERSE)

# how the stop distance is measured when the entry is reversed
ANCHOR_RANGE = "range"     # risk = sl_range_mult x range height
ANCHOR_MIRROR = "mirror"   # risk = the original trade's own stop distance
ANCHORS = (ANCHOR_RANGE, ANCHOR_MIRROR)


@dataclass
class ReversalSettings(EngineSettings):
    """One reversal configuration.

    sl_range_mult
        Stop distance as a multiple of the opening range height. 0.5 reproduces
        the breakout engine's `mid_range` and 1.0 its `full_range`, exactly —
        trade for trade. Any positive value is allowed: 0.25, 0.75, 1.5, 2.0.

    direction
        "reverse" fades the breakout — a close above the range high SELLS.
        "forward" is the ordinary breakout direction, so the same stop
        multiplier can be measured both ways. Run it as the control arm: if
        forward is profitable too, the stop distance is doing the work, not the
        fade.

    sl_anchor
        Consulted only when reversing, because a reversed trade cannot reuse the
        original stop LEVEL — that level sits on the wrong side of the entry and
        the broker would close the trade instantly.

        "range"  risk = sl_range_mult x range height, measured from the fill.
                 Independent of how far the breakout bar overshot.
        "mirror" risk = the exact distance the original trade would have taken,
                 overshoot included, mirrored to the other side.

    max_trades_per_session
        Trades before the session stops. 1 = R, 2 = RR, 3 = RRR, 0 = unlimited.
        Copied onto the session's own `max_trades_per_session` field when the
        settings are applied, so the core enforces it exactly as it always has.

    order_tag
        Prefix written into the order comment, so a reversed position is
        identifiable on the broker side.
    """

    # `max_trades` was the old spelling; `reverse: bool` the old direction switch
    ALIASES: ClassVar[Dict[str, str]] = {
        "max_trades": "max_trades_per_session",
        "mult": "sl_range_mult",
        "anchor": "sl_anchor",
        "tag": "order_tag",
    }

    sl_range_mult: float = 0.5
    direction: str = REVERSE
    sl_anchor: str = ANCHOR_RANGE
    max_trades_per_session: int = 0
    order_tag: str = "REV"

    # ------------------------------------------------------------------
    @property
    def reverse(self) -> bool:
        return self.direction == REVERSE

    def validate(self) -> None:
        if not isinstance(self.sl_range_mult, (int, float)) or \
                self.sl_range_mult <= 0:
            raise ValueError(
                f"sl_range_mult must be greater than 0 (got "
                f"{self.sl_range_mult!r}). 0.5 = the breakout engine's "
                f"mid_range, 1.0 = its full_range.")
        self.sl_range_mult = float(self.sl_range_mult)

        direction = str(self.direction).strip().lower()
        if direction not in DIRECTIONS:
            raise ValueError(
                f"direction must be one of {', '.join(DIRECTIONS)} "
                f"(got {self.direction!r}).")
        self.direction = direction

        anchor = str(self.sl_anchor).strip().lower()
        if anchor not in ANCHORS:
            raise ValueError(
                f"sl_anchor must be one of {', '.join(ANCHORS)} "
                f"(got {self.sl_anchor!r}).")
        self.sl_anchor = anchor

        if int(self.max_trades_per_session) < 0:
            raise ValueError(
                "max_trades_per_session cannot be negative (0 = unlimited).")
        self.max_trades_per_session = int(self.max_trades_per_session)

    # ------------------------------------------------------------------
    def run_name(self) -> str:
        """File-safe description, e.g. `REV_SL1p5_RR`."""
        mult = f"{self.sl_range_mult:g}".replace(".", "p")
        head = self.order_tag if self.reverse else "FWD"
        cap = {0: "ALL", 1: "R", 2: "RR", 3: "RRR"}.get(
            self.max_trades_per_session, f"N{self.max_trades_per_session}")
        return f"{head}_SL{mult}_{cap}"

    def describe(self) -> str:
        note = {0.5: "  (identical to the breakout engine's mid_range)",
                1.0: "  (identical to its full_range)"}.get(
                    float(self.sl_range_mult), "")
        base = ("fade the breakout" if self.reverse
                else "the ordinary breakout direction")
        anchor = (f", stop measured from the "
                  + ("range height" if self.sl_anchor == ANCHOR_RANGE
                     else "mirrored original stop")) if self.reverse else ""
        cap = ("unlimited trades per session"
               if not self.max_trades_per_session else
               f"first {self.max_trades_per_session} trade(s) of the session only")
        return (f"{base}; stop = {self.sl_range_mult:g} x range{note}"
                f"{anchor}; {cap}")

    # ------------------------------------------------------------------
    def apply_to_session(self, cfg):
        """Write these settings onto one session's `StrategyConfig`.

        `max_trades_per_session` and `comment` are real core fields, so they are
        set directly; everything else goes into `engine_options`, where this
        engine's strategy reads it back.
        """
        self.validate()
        cfg.engine = "reversal"
        cfg.engine_options = self.to_options()
        cfg.max_trades_per_session = int(self.max_trades_per_session)
        base = str(cfg.comment or "").strip()
        prefix = self.order_tag if self.reverse else "FWD"
        if not base.startswith(prefix):
            cfg.comment = f"{prefix} {base}".strip()
        return cfg

    def apply_to(self, app):
        for session in app.enabled_sessions():
            self.apply_to_session(session)
        return app
