"""The `orb_reverse` strategy — fade the opening-range breakout.

A subclass of the `orb` strategy that overrides exactly two methods.
Everything else — the range build, the breakout test, the arming and re-entry
rules, the news filter, the stop time, the fill model — is inherited and runs
unmodified.

    `_stop_price`   where the stop goes
    `_open_trade`   which way the order goes

It reaches the engine through the registry (`orb/registry.py`), selected by
`engine: orb_reverse` on a session. It used to get there by rebinding a module
global, which was process-wide and therefore made it impossible to run this
engine and the `orb` engine at the same time.
"""
from __future__ import annotations

from ..base import settings_of
from ..orb.strategy import OrbStrategy
from .settings import ANCHOR_MIRROR, OrbReverseSettings


class OrbReverseStrategy(OrbStrategy):
    """Fade the opening-range breakout, with a range-multiple stop.

    The stop
    --------
    `range_size = range_high - range_low`. The stop distance is
    `sl_range_mult x range_size`.

    Running the ORDINARY direction (`direction: forward`) the stop is a LEVEL
    measured inward from the side that broke:

        BUY   stop = range_high - mult x range_size
        SELL  stop = range_low  + mult x range_size

    which at mult 0.5 is the range midpoint and at mult 1.0 is the opposite side
    — the `orb` engine's `mid_range` and `full_range`, reproduced exactly.

    Running REVERSED the same level cannot be used. A reversed SELL entered on a
    break ABOVE the range would get a stop below its entry, which is not a stop
    at all: the broker fills it immediately and every trade prints breakeven. So
    a reversed trade keeps the stop DISTANCE and puts it on the far side of the
    entry:

        reversed BUY   stop = entry - risk
        reversed SELL  stop = entry + risk

    with `risk` taken either from the range (`sl_anchor: range`, the default:
    `mult x range_size`, independent of the breakout overshoot) or by mirroring
    what the original trade's own stop distance would have been
    (`sl_anchor: mirror`).

    The take profit is unchanged: `risk_reward x risk`, applied by the inherited
    `_open_trade`.
    """

    def __init__(self, cfg, broker, store=None, logger=None):
        # read once, from the one place the defaults live
        self.settings: OrbReverseSettings = settings_of(cfg, OrbReverseSettings)
        super().__init__(cfg, broker, store=store, logger=logger)

    # ------------------------------------------------------------------
    @property
    def sl_range_mult(self) -> float:
        return self.settings.sl_range_mult

    @property
    def reverse(self) -> bool:
        return self.settings.reverse

    @property
    def sl_anchor(self) -> str:
        return self.settings.sl_anchor

    @property
    def range_size(self) -> float:
        return abs(self.range_high - self.range_low)

    # ------------------------------------------------------------------
    def _range_stop_level(self, is_buy: bool) -> float:
        """The stop LEVEL for a trade going `is_buy`, measured from the side of
        the range it broke. mult 0.5 -> midpoint, 1.0 -> opposite side."""
        distance = self.sl_range_mult * self.range_size
        return ((self.range_high - distance) if is_buy
                else (self.range_low + distance))

    def _stop_loss_label(self) -> str:
        """This engine ignores `sl_mode`; the stop is a multiple of the range.
        Journal text only — `_stop_price` below is what actually places it."""
        if not self.reverse:
            return super()._stop_loss_label() + " (forward)"
        if self.sl_anchor == ANCHOR_MIRROR:
            return "mirrored range distance"
        return f"{self.sl_range_mult:g} x range"

    def _stop_price(self, is_buy: bool) -> float:
        if not self.reverse:
            return self._range_stop_level(is_buy)

        # `is_buy` arrives already flipped by `_open_trade`, so the direction the
        # breakout actually pointed is its opposite.
        price = self.broker.reference_price(is_buy)
        if self.sl_anchor == ANCHOR_MIRROR:
            risk = abs(price - self._range_stop_level(not is_buy))
        else:
            risk = self.sl_range_mult * self.range_size
        return (price - risk) if is_buy else (price + risk)

    # ------------------------------------------------------------------
    def _open_trade(self, is_buy: bool) -> None:
        super()._open_trade((not is_buy) if self.reverse else is_buy)

    # ------------------------------------------------------------------
    def _banner(self) -> None:
        """The inherited banner, then the reversal settings — so the journal
        says what actually ran instead of the `sl_mode` this engine ignores."""
        super()._banner()
        self.log.info("=" * 62)
        self.log.info("REVERSAL ENGINE")
        self.log.info(f"   {self.settings.describe()}")
        self.log.info("   NOTE          sl_mode is ignored by this engine; "
                      "sl_range_mult replaces it")
        self.log.info("=" * 62)
