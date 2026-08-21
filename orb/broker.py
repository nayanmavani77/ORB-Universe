"""Broker abstraction.

`SimBroker`  — used by the backtester; simulates fills, intrabar SL/TP and P&L.
`MT5Broker`  — used for live trading through the MetaTrader5 Python API.

The strategy never talks to either directly beyond this interface, so the
trading logic is byte-for-byte the same in backtest and live.
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from .bars import Bar
from .config import SymbolSpec


# ==========================================================================
@dataclass
class Position:
    ticket: int
    is_buy: bool
    lots: float
    entry_price: float
    entry_time: datetime
    sl: float = 0.0
    tp: float = 0.0
    comment: str = ""
    magic: int = 0
    # bookkeeping used by the report
    session_name: str = ""
    #: which instrument this position is on. "" for a single-instrument run.
    instrument: str = ""
    session_start: Optional[datetime] = None
    range_high: float = 0.0
    range_low: float = 0.0
    range_mid: float = 0.0
    trade_no_in_session: int = 0
    entry_commission: float = 0.0
    #: The risk this trade was OPENED with, in price units. Recorded once, at
    #: the fill, because `sl` may move afterwards -- break-even puts it ON the
    #: entry price, which would make `abs(entry - sl)` zero and every
    #: R-multiple computed from it a lie. R means "times the risk I actually
    #: took", so it is measured against this and nothing else.
    initial_risk: float = 0.0
    #: Where the SIGNAL fired, in FEED space. Equal to `entry_price` whenever
    #: signal and execution are the same instrument (every backtest, and live
    #: on one symbol). It differs only under `translate_levels`, where the feed
    #: quotes tens of dollars away from the broker -- and any test measured
    #: against feed bars, break-even included, has to use this one.
    signal_entry: float = 0.0
    #: Price the stop was moved to by break-even. 0.0 = never moved, which is
    #: also how "has break-even already fired" is answered.
    breakeven_at: float = 0.0
    #: The bar during which break-even moved the stop. A bar's OPEN is normally
    #: tested for a gap straight through the stop, but a stop placed part-way
    #: through a bar did not exist at that bar's open and cannot be gapped by
    #: it. On the entry bar the two prices are the same number -- the fill IS
    #: the open, and break-even puts the stop on the fill -- so without this
    #: the trade would close the instant break-even armed.
    breakeven_bar: Optional[datetime] = None


@dataclass
class ClosedTrade:
    ticket: int
    direction: str
    lots: float
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    sl: float
    tp: float
    exit_reason: str
    gross_profit: float
    commission: float
    net_profit: float
    balance_after: float
    session_name: str
    session_start: Optional[datetime]
    range_high: float
    range_low: float
    range_mid: float
    trade_no_in_session: int
    r_multiple: float
    #: risk at entry, in price units -- what `r_multiple` is measured against
    initial_risk: float = 0.0
    #: did the stop get moved to break-even before this trade ended?
    breakeven: bool = False
    #: which instrument the trade was on. Empty for a single-instrument run.
    instrument: str = ""


# ==========================================================================
class Broker(ABC):
    spec: SymbolSpec

    # --- symbol info ----------------------------------------------------
    @property
    def digits(self) -> int:
        return self.spec.digits

    @property
    def point(self) -> float:
        return self.spec.point

    @property
    def stops_level_price(self) -> float:
        """SYMBOL_TRADE_STOPS_LEVEL * SYMBOL_POINT"""
        return self.spec.stops_level_points * self.spec.point

    def normalize_lot(self, lot: float, instrument: str = "") -> float:
        """MQL5 NormalizeLot(), against THIS instrument's volume steps."""
        spec = self.spec_for(instrument)
        step = spec.volume_step or 0.01
        lot = math.floor(lot / step) * step
        lot = max(lot, spec.volume_min)
        lot = min(lot, spec.volume_max)
        return round(lot, 2)

    def normalize_price(self, price: float, instrument: str = "") -> float:
        return round(price, self.spec_for(instrument).digits)

    # --- required interface ---------------------------------------------
    @abstractmethod
    def ask(self) -> float: ...

    @abstractmethod
    def bid(self) -> float: ...

    @abstractmethod
    def positions_count(self) -> int: ...

    @abstractmethod
    def open_market(self, is_buy: bool, lots: float, sl: float,
                    comment: str, magic: int = 0,
                    price: Optional[float] = None
                    ) -> Tuple[bool, Optional[Position], str]: ...

    # -- per-instrument access --------------------------------------------
    # Defaults that make a single-instrument broker answer correctly without
    # knowing instruments exist at all. `view()` below is how a session gets a
    # broker bound to ITS instrument, so no strategy code takes an extra
    # argument anywhere.
    def spec_for(self, instrument: str = "") -> SymbolSpec:
        return self.spec

    def position_for(self, instrument: str = ""):
        return getattr(self, "position", None)

    def view(self, instrument: str = "") -> "Broker":
        """This broker, bound to one instrument.

        The strategy calls `broker.ask()`, `broker.positions_count()` and so on
        with no arguments and must keep doing so — the trading rules are the
        one thing that may not change. A view supplies the instrument on every
        call, so one account can carry several instruments while each strategy
        still believes it has a broker to itself.
        """
        if not instrument:
            return self
        return InstrumentView(self, instrument)

    @abstractmethod
    def modify(self, position: Position, sl: float, tp: float) -> Tuple[bool, str]: ...

    @abstractmethod
    def close_all(self, reason: str) -> None: ...

    def price_for(self, is_buy: bool, instrument: str = "") -> float:
        """Where an order would actually FILL — the execution instrument."""
        return self.ask(instrument) if is_buy else self.bid(instrument)

    def reference_price(self, is_buy: bool, instrument: str = "") -> float:
        """Where the SIGNAL was generated — the data-feed instrument.

        For a backtest, and for live trading on the same instrument the data
        came from, these are one and the same. They diverge only when the
        signal is computed on one instrument and executed on another (CME GC
        deciding, spot XAUUSD executing), which is what `translate_levels`
        exists for.
        """
        return self.price_for(is_buy, instrument)

    def trades_opened_since(self, magic: int, since: datetime,
                            instrument: str = ""):
        """How many positions this magic opened since `since`. None = unknown.

        Exists so a restart cannot reset `max_trades_per_session`. The counter
        lives in memory, so a process that restarts mid-session used to begin
        again at zero — restart three times on a 2-trade cap and you get six
        trades. Only the broker knows what actually happened, so only the
        broker can answer.

        `None` means "could not determine", which is NOT the same as zero: the
        caller blocks the session rather than risk exceeding the cap.

        The default is 0 — correct for any broker that cannot restart
        mid-session, which is every simulated one.
        """
        return 0

    #: Does this broker take its price from the BARS it is fed, or from a live
    #: quote it can ask for at any instant?
    #:
    #: `SimBroker` fills at the open of the bar the EA reacted on, so it can
    #: only price a decision once the NEXT bar exists. `MT5Broker` reads a tick
    #: whenever it is asked, so it can price a decision the moment a bar
    #: closes. `Engine.eager_close` — acting the instant a bar completes rather
    #: than waiting for its successor — is only possible on the second kind,
    #: which is why the engine is gated on this rather than on live-vs-backtest.
    prices_from_bars: bool = True

    # A price LEVEL from the feed cannot be sent to a broker quoting a
    # different instrument: GC and XAUUSD track each other but sit tens of
    # dollars apart. Only the DISTANCES survive the crossing. When this is
    # true the strategy carries SL/TP across as distances measured from the
    # real fill; when false it uses the feed's absolute levels, which is
    # correct — and exactly what the MQL5 EA does — when both sides are the
    # same instrument.
    translate_levels: bool = False

    # --- engine hooks ----------------------------------------------------
    # A simulated broker needs to know where price is before the tick runs,
    # and needs to walk the bar for SL/TP afterwards.  A live broker gets both
    # from the market and the server, so both are no-ops.
    def sync_market(self, bar: Bar, now: datetime) -> None:
        """Called by the engine BEFORE OnTick, so the tick sees a price."""
        return None

    def settle_bar(self, bar: Bar) -> None:
        """Called by the engine AFTER OnTick, to resolve SL/TP inside the bar."""
        return None


# ==========================================================================
class InstrumentView:
    """One instrument's face of a shared account.

    Every call the strategy makes — `ask()`, `positions_count()`,
    `open_market(...)`, `digits` — is forwarded with this instrument attached.
    That is what lets several instruments share one balance without a single
    line of strategy code learning that instruments exist: `MultiEngine` hands
    each session a view, and the session sees what looks like its own broker.

    Anything not listed here falls through to the account broker unchanged, so
    balance, trade list and equity curve stay shared and singular — which is
    the point of a portfolio.
    """

    def __init__(self, broker: "Broker", instrument: str):
        self._broker = broker
        self.instrument = instrument

    # --- prices, all scoped -------------------------------------------
    def ask(self) -> float:
        return self._broker.ask(self.instrument)

    def bid(self) -> float:
        return self._broker.bid(self.instrument)

    def price_for(self, is_buy: bool) -> float:
        return self._broker.price_for(is_buy, self.instrument)

    def reference_price(self, is_buy: bool) -> float:
        return self._broker.reference_price(is_buy, self.instrument)

    def basis(self, is_buy: bool) -> float:
        fn = getattr(self._broker, "basis", None)
        return fn(is_buy, self.instrument) if callable(fn) else 0.0

    # --- positions, all scoped ----------------------------------------
    def positions_count(self) -> int:
        return self._broker.positions_count(self.instrument)

    def position_for(self, instrument: str = "") -> Optional[Position]:
        """THIS instrument's open position.

        Without this override `__getattr__` would forward to the account
        broker with no instrument, which answers for whichever position it
        keeps under the empty key — so a GC session could read an ES position
        and move ITS stop. Every other position accessor here is scoped; this
        one has to be too.
        """
        return self._broker.position_for(instrument or self.instrument)

    def open_market(self, is_buy: bool, lots: float, sl: float, comment: str,
                    magic: int = 0, price: Optional[float] = None):
        return self._broker.open_market(is_buy, lots, sl, comment, magic=magic,
                                        instrument=self.instrument, price=price)

    def close_all(self, reason: str) -> None:
        self._broker.close_all(reason, instrument=self.instrument)

    def trades_opened_since(self, magic: int, since: datetime):
        """This instrument's openings only — see `MT5Broker` for why the
        symbol filter matters on a shared account."""
        return self._broker.trades_opened_since(magic, since,
                                                instrument=self.instrument)

    # --- contract detail ------------------------------------------------
    @property
    def spec(self) -> SymbolSpec:
        return self._broker.spec_for(self.instrument)

    @property
    def digits(self) -> int:
        return self.spec.digits

    @property
    def point(self) -> float:
        return self.spec.point

    def normalize_price(self, price: float) -> float:
        return self._broker.normalize_price(price, self.instrument)

    def normalize_lot(self, lots: float) -> float:
        return self._broker.normalize_lot(lots, self.instrument)

    def __getattr__(self, name):
        # everything else — modify, translate_levels, trades_opened_since,
        # min_stop_distance, the account itself — is shared, so forward it
        return getattr(self._broker, name)


# ==========================================================================
class SimBroker(Broker):
    """Backtest broker.

    Fill model
      * entry  : at the current market price (open of the bar on which the EA
                 reacts), plus spread on buys, plus slippage against you.
      * SL/TP  : checked bar by bar on the base resolution.  If a bar gaps
                 straight through a level the fill happens at the bar OPEN.
                 If both SL and TP sit inside one bar, `pessimistic_intrabar`
                 decides — default assumes the stop was hit first.
    """

    def __init__(self, spec: SymbolSpec, initial_balance: float = 100000.0,
                 spread_points: float = 0.0, slippage_points: float = 0.0,
                 commission_per_lot_per_side: float = 0.0,
                 pessimistic_intrabar: bool = True, logger=None):
        self.spec = spec
        self.balance = float(initial_balance)
        self.initial_balance = float(initial_balance)
        self.spread = spread_points * spec.point
        self.slippage = slippage_points * spec.point
        self.commission = commission_per_lot_per_side
        self.pessimistic = pessimistic_intrabar
        self.log = logger

        self._next_ticket = 1
        # ONE POSITION PER INSTRUMENT, keyed by instrument name. A
        # single-instrument run uses the "" key throughout and behaves exactly
        # as it always did — one slot, one price, one spec. Several
        # instruments get a slot each, because GC New York and ES New York are
        # the same hours and refusing the second would make multi-instrument
        # trading impossible.
        self.positions: Dict[str, Position] = {}
        self.specs: Dict[str, SymbolSpec] = {"": spec}
        self.trades: List[ClosedTrade] = []
        self.equity_curve: List[Tuple[datetime, float]] = []

        self._prices: Dict[str, float] = {}
        self._now: Optional[datetime] = None
        self.on_exit = None  # callback(ClosedTrade) — used for journal lines
        # the simulated broker executes the very instrument the bars describe,
        # so signal space and execution space are identical and levels carry
        # across unchanged
        self.translate_levels = False
        # fills come from the bars this broker is fed, so a decision cannot be
        # priced until the next bar exists — see `Broker.prices_from_bars`
        self.prices_from_bars = True

    # -- per-instrument accessors -----------------------------------------
    def spec_for(self, instrument: str = "") -> SymbolSpec:
        """This instrument's contract spec, falling back to the run's default."""
        return self.specs.get(instrument or "", self.spec)

    def add_instrument(self, instrument: str, spec: SymbolSpec,
                       mt5_symbol: str = "") -> None:
        """Register an instrument's contract details before trading it.

        `mt5_symbol` is accepted and ignored: a simulated broker has no
        terminal to route to. The signature matches `MT5Broker` so callers do
        not have to know which broker they hold.
        """
        self.specs[instrument or ""] = spec

    def position_for(self, instrument: str = "") -> Optional[Position]:
        return self.positions.get(instrument or "")

    @property
    def position(self) -> Optional[Position]:
        """The one open position. Kept for single-instrument callers, which is
        every caller that predates several instruments."""
        if not self.positions:
            return None
        if "" in self.positions:
            return self.positions[""]
        return next(iter(self.positions.values()))

    @position.setter
    def position(self, value: Optional[Position]) -> None:
        if value is None:
            self.positions.pop("", None)
        else:
            self.positions[getattr(value, "instrument", "") or ""] = value

    # -- market context ---------------------------------------------------
    def set_market(self, price: float, now: datetime,
                   instrument: str = "") -> None:
        self._prices[instrument or ""] = float(price)
        self._now = now

    def price_of(self, instrument: str = "") -> float:
        return self._prices.get(instrument or "", 0.0)

    # engine hooks
    def sync_market(self, bar: Bar, now: datetime) -> None:
        self.set_market(bar.open, now, getattr(bar, "instrument", ""))

    def settle_bar(self, bar: Bar) -> None:
        self.process_bar(bar)

    def ask(self, instrument: str = "") -> float:
        return self.price_of(instrument) + self.spread

    def bid(self, instrument: str = "") -> float:
        return self.price_of(instrument)

    # -- interface --------------------------------------------------------
    def positions_count(self, instrument: Optional[str] = None) -> int:
        """Open positions. With an instrument, only that one's — which is what
        the strategy asks, because GC being open must not block ES."""
        if instrument is None:
            return len(self.positions)
        return 1 if self.positions.get(instrument or "") else 0

    def open_market(self, is_buy: bool, lots: float, sl: float, comment: str,
                    magic: int = 0, instrument: str = "",
                    price: Optional[float] = None):
        """Open at the market, or AT A GIVEN PRICE.

        `price` is how a pullback entry fills at the level it was waiting for.
        The strategy detects the touch inside the bar — the bar's low reached
        the range high — and that level, not the bar's open, is where a resting
        limit order would have filled. Passing it here keeps the simulated fill
        honest instead of pricing the trade wherever the bar happened to start.

        The touch is already proven by the bar's own high/low before this is
        called, so the fill is not optimistic: price genuinely traded there.
        Slippage still applies, in the same direction it always does.
        """
        key = instrument or ""
        if self.positions.get(key) is not None:
            return False, None, "position already open"
        if price is None:
            price = self.price_for(is_buy, instrument)
        if price <= 0:
            return False, None, "no price"
        fill = price + (self.slippage if is_buy else -self.slippage)
        fill = self.normalize_price(fill, key)
        pos = Position(
            ticket=self._next_ticket, is_buy=is_buy, lots=lots,
            entry_price=fill, entry_time=self._now, sl=sl, tp=0.0,
            comment=comment, magic=int(magic),
            entry_commission=self.commission * lots,
        )
        pos.instrument = key
        self._next_ticket += 1
        self.positions[key] = pos
        return True, pos, ""

    def modify(self, position: Position, sl: float, tp: float):
        for pos in self.positions.values():
            if pos.ticket == position.ticket:
                pos.sl, pos.tp = sl, tp
                return True, ""
        return False, "position not found"

    def close_all(self, reason: str, instrument: Optional[str] = None) -> None:
        """Flatten. With an instrument, only that one — a session reaching its
        stop time must not close another instrument's open trade."""
        keys = ([instrument or ""] if instrument is not None
                else list(self.positions))
        for key in keys:
            pos = self.positions.get(key)
            if pos is None:
                continue
            price = self.bid(key) if pos.is_buy else self.ask(key)
            price = price - self.slippage if pos.is_buy else price + self.slippage
            self._settle(pos, self.normalize_price(price, key), self._now, reason)

    # -- bar-by-bar position management ----------------------------------
    def process_bar(self, bar: Bar) -> None:
        """Walk one base bar and fire SL / TP if touched.

        Only the position on the instrument THIS bar describes — an ES bar
        must never be walked against a GC position.
        """
        key = getattr(bar, "instrument", "") or ""
        pos = self.positions.get(key)
        if pos is None:
            self.mark_equity(bar.time, bar.close, key)
            return
        if pos.entry_time is not None and bar.time < pos.entry_time:
            self.mark_equity(bar.time, bar.close, key)
            return

        sl_hit = tp_hit = False
        sl_price = tp_price = 0.0
        # Was the stop already in place when this bar opened? If break-even
        # moved it DURING this bar, no -- so the open cannot have gapped
        # through it. The intrabar high/low tests below still apply: those are
        # the touch that Story A resolves pessimistically.
        sl_at_open = pos.breakeven_bar != bar.time

        if pos.is_buy:
            # SL is below, TP is above; buys exit on the bid
            if pos.sl > 0:
                if sl_at_open and bar.open <= pos.sl:
                    sl_hit, sl_price = True, bar.open
                elif bar.low <= pos.sl:
                    sl_hit, sl_price = True, pos.sl - self.slippage
            if pos.tp > 0:
                if bar.open >= pos.tp:
                    tp_hit, tp_price = True, bar.open
                elif bar.high >= pos.tp:
                    tp_hit, tp_price = True, pos.tp
        else:
            # sells exit on the ask
            hi_ask = bar.high + self.spread
            lo_ask = bar.low + self.spread
            op_ask = bar.open + self.spread
            if pos.sl > 0:
                if sl_at_open and op_ask >= pos.sl:
                    sl_hit, sl_price = True, op_ask
                elif hi_ask >= pos.sl:
                    sl_hit, sl_price = True, pos.sl + self.slippage
            if pos.tp > 0:
                if op_ask <= pos.tp:
                    tp_hit, tp_price = True, op_ask
                elif lo_ask <= pos.tp:
                    tp_hit, tp_price = True, pos.tp

        if sl_hit and tp_hit:
            if self.pessimistic:
                tp_hit = False
            else:
                sl_hit = False

        if sl_hit:
            # A stop that was moved to the entry is not a losing stop, and a
            # report that calls it one is misleading. Naming it separately also
            # makes the exit-reason breakdown answer "how often did break-even
            # actually save / cost me?" without any extra plumbing.
            self._settle(pos, self.normalize_price(sl_price, key), bar.time,
                         "BREAK EVEN stop hit" if pos.breakeven_at
                         else "STOP LOSS hit")
        elif tp_hit:
            self._settle(pos, self.normalize_price(tp_price, key),
                         bar.time, "TAKE PROFIT hit")

        self.mark_equity(bar.time, bar.close, key)

    # -- P&L --------------------------------------------------------------
    def _settle(self, pos: Position, exit_price: float,
                exit_time: Optional[datetime], reason: str) -> None:
        key = getattr(pos, "instrument", "") or ""
        spec = self.spec_for(key)
        direction = 1.0 if pos.is_buy else -1.0
        gross = (exit_price - pos.entry_price) * direction * pos.lots * \
            spec.value_per_price_unit
        commission = pos.entry_commission + self.commission * pos.lots
        net = gross - commission
        self.balance += net
        # R is measured against the risk the trade was OPENED with, not
        # against wherever the stop happens to sit at the exit. Break-even puts
        # the stop ON the entry, so `abs(entry - sl)` would be zero and every
        # winner that passed through break-even would report 0R. The fallback
        # keeps any position opened without a recorded risk behaving exactly as
        # it always did.
        risk = pos.initial_risk if pos.initial_risk > 0 else (
            abs(pos.entry_price - pos.sl) if pos.sl > 0 else 0.0)
        r_mult = ((exit_price - pos.entry_price) * direction / risk) if risk > 0 else 0.0
        trade = ClosedTrade(
            ticket=pos.ticket,
            direction="BUY" if pos.is_buy else "SELL",
            lots=pos.lots,
            entry_time=pos.entry_time,
            entry_price=pos.entry_price,
            exit_time=exit_time,
            exit_price=exit_price,
            sl=pos.sl, tp=pos.tp,
            exit_reason=reason,
            gross_profit=gross,
            commission=commission,
            net_profit=net,
            balance_after=self.balance,
            session_name=pos.session_name,
            session_start=pos.session_start,
            range_high=pos.range_high,
            range_low=pos.range_low,
            range_mid=pos.range_mid,
            trade_no_in_session=pos.trade_no_in_session,
            r_multiple=r_mult,
            initial_risk=risk,
            breakeven=bool(pos.breakeven_at),
            instrument=key,
        )
        self.trades.append(trade)
        self.positions.pop(key, None)
        if self.on_exit:
            self.on_exit(trade)

    def floating_pnl(self, price: Optional[float] = None,
                     instrument: Optional[str] = None) -> float:
        """Open profit. Across EVERY instrument, each marked at its own last
        price — a portfolio's equity is not one position's.

        `price` overrides the mark for the instrument named, which is how
        `process_bar` values the bar it is currently walking.
        """
        total = 0.0
        for key, pos in self.positions.items():
            mark = price if (instrument is not None and key == instrument) \
                else self.price_of(key)
            if not mark:
                continue
            d = 1.0 if pos.is_buy else -1.0
            total += (mark - pos.entry_price) * d * pos.lots * \
                self.spec_for(key).value_per_price_unit
        return total

    def mark_equity(self, when: datetime, price: Optional[float] = None,
                    instrument: Optional[str] = None) -> None:
        self.equity_curve.append(
            (when, self.balance + self.floating_pnl(price, instrument)))


# ==========================================================================
class MT5Broker(Broker):
    """Live execution through the official `MetaTrader5` Python package.

    Mirrors CTrade usage in the EA: market order with SL attached at send time,
    TP applied afterwards from the real fill price via PositionModify.
    """

    def __init__(self, mt5_cfg, spec: SymbolSpec, magic, logger=None):
        try:
            import MetaTrader5 as mt5  # noqa: N813
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "The MetaTrader5 package is required for live trading.\n"
                "Install it on Windows with:  pip install MetaTrader5"
            ) from exc
        self.mt5 = mt5
        # a live quote is available at any instant, so a decision can be priced
        # the moment a bar closes — see `Broker.prices_from_bars`
        self.prices_from_bars = False
        self.cfg = mt5_cfg
        self.spec = spec
        # `magic` may be a single number or every session's magic. The EA owns
        # ALL of them: it must see all its own positions to enforce one trade
        # at a time, while each order still carries its own session's magic so
        # MetaTrader can tell them apart.
        if isinstance(magic, (list, tuple, set, frozenset)):
            self.magics = {int(m) for m in magic}
        else:
            self.magics = {int(magic)}
        self.magic = min(self.magics)
        self.log = logger
        self.symbol = mt5_cfg.symbol
        # instrument key -> the symbol in the terminal, and its spec. Empty
        # means the single-instrument shape: everything answers for
        # `mt5_cfg.symbol` exactly as it always did.
        self.symbols: Dict[str, str] = {"": self.symbol}
        #: last feed price PER INSTRUMENT — a gold bar must not become the
        #: reference price for an ES order on the same account
        self._feed_prices: Dict[str, float] = {}
        self.specs: Dict[str, SymbolSpec] = {"": spec}
        self.translate_levels = bool(getattr(mt5_cfg, "translate_levels", True))
        # last price seen on the DATA FEED (CME), kept apart from the broker's
        # own quote so distances can be measured in the space the signal was
        # computed in
        self._feed_price: Optional[float] = None
        self._connect()
        self._load_symbol_spec()

    # -- setup ------------------------------------------------------------
    def _connect(self) -> None:
        mt5 = self.mt5
        kwargs = {}
        if self.cfg.terminal_path:
            kwargs["path"] = self.cfg.terminal_path
        if self.cfg.login:
            kwargs.update(login=int(self.cfg.login),
                          password=self.cfg.password,
                          server=self.cfg.server)
        if not mt5.initialize(**kwargs):
            raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")
        if not mt5.symbol_select(self.symbol, True):
            raise RuntimeError(f"MT5 could not select symbol '{self.symbol}': "
                               f"{mt5.last_error()}")
        info = mt5.account_info()
        if info and self.log:
            self.log.info(f"MT5 connected | account {info.login} | {info.server} "
                          f"| balance {info.balance:.2f} {info.currency}")

    def _load_symbol_spec(self) -> None:
        si = self.mt5.symbol_info(self.symbol)
        if si is None:
            raise RuntimeError(f"symbol_info('{self.symbol}') returned None")
        self.spec.name = self.symbol
        self.spec.digits = si.digits
        self.spec.point = si.point
        self.spec.tick_size = si.trade_tick_size or si.point
        self.spec.stops_level_points = si.trade_stops_level
        self.spec.volume_min = si.volume_min
        self.spec.volume_max = si.volume_max
        self.spec.volume_step = si.volume_step
        if si.trade_tick_size:
            self.spec.value_per_price_unit = si.trade_tick_value / si.trade_tick_size
        if self.log:
            self.log.info(
                f"Symbol {self.symbol} | digits {si.digits} | point {si.point} "
                f"| stops level {si.trade_stops_level} | vol {si.volume_min}"
                f"/{si.volume_step}/{si.volume_max}")

    def sync_market(self, bar: Bar, now: datetime) -> None:
        """The engine hands every feed bar through here. A live broker does not
        need it to price an order, but it IS how the feed's price reaches the
        broker — which is what lets SL/TP distances be measured in signal space
        rather than against the broker's own quote."""
        # kept PER INSTRUMENT: a gold bar must not become the reference price
        # for an ES order sitting on the same account
        self._feed_prices[getattr(bar, "instrument", "") or ""] = float(bar.close)
        self._feed_price = float(bar.close)

    def reference_price(self, is_buy: bool, instrument: str = "") -> float:
        """The feed's price for THIS instrument, NOT the broker's. Falls back
        to the broker quote only if no bar has arrived yet."""
        feed = self._feed_prices.get(instrument or "", 0.0)
        if feed and feed > 0:
            return feed
        return self.price_for(is_buy, instrument)

    def basis(self, is_buy: bool, instrument: str = "") -> float:
        """Execution price minus feed price — how far the two instruments sit
        apart right now. Logged on every fill so a drifting basis is visible."""
        ref = self.reference_price(is_buy, instrument)
        return self.price_for(is_buy, instrument) - ref if ref else 0.0

    def server_time(self) -> datetime:
        """Now, as MT5 sees it. NAIVE and in UTC.

        `utcfromtimestamp`/`utcnow` are deprecated and are scheduled for
        removal, so the aware form is used and the tzinfo stripped. It stays
        naive deliberately: every timestamp in this system is naive-UTC, and
        returning an aware value here would make it uncomparable with the bar
        times it is checked against.
        """
        tick = self.mt5.symbol_info_tick(self.symbol)
        if tick:
            return datetime.fromtimestamp(tick.time, tz=timezone.utc).replace(
                tzinfo=None)
        return datetime.now(timezone.utc).replace(tzinfo=None)

    # -- per-instrument -----------------------------------------------
    def symbol_for(self, instrument: str = "") -> str:
        """The terminal symbol this instrument trades."""
        return self.symbols.get(instrument or "", self.symbol)

    def spec_for(self, instrument: str = "") -> SymbolSpec:
        return self.specs.get(instrument or "", self.spec)

    def add_instrument(self, instrument: str, spec: SymbolSpec,
                       mt5_symbol: str = "") -> None:
        """Register an instrument, and read its real contract details from the
        terminal — digits, tick size and volume steps differ per symbol, and
        guessing them wrong misprices every order."""
        key = instrument or ""
        mt5_symbol = mt5_symbol or spec.name or self.symbol
        self.symbols[key] = mt5_symbol
        self.specs[key] = spec
        prev_symbol, prev_spec = self.symbol, self.spec
        try:
            self.symbol, self.spec = mt5_symbol, spec
            self.mt5.symbol_select(mt5_symbol, True)
            self._load_symbol_spec()
        except Exception as exc:                       # pragma: no cover
            if self.log:
                self.log.warn(f"Could not read {mt5_symbol} from the terminal "
                              f"({exc!r}); using the configured contract "
                              f"details for it.")
        finally:
            self.symbol, self.spec = prev_symbol, prev_spec

    def trades_opened_since(self, magic: int, since: datetime,
                            instrument: str = ""):
        """Count position OPENINGS with this magic since `since`.

        Counts entry deals rather than closed positions, so a trade that is
        still open counts too — it is one of the session's allowance either
        way. Returns None if MT5 will not answer, which the strategy treats as
        "assume the cap may be spent" rather than "assume none were taken".
        """
        try:
            # MT5 wants naive local-ish datetimes here, the same shape
            # `server_time` returns, and an `until` safely in the future so a
            # deal recorded a moment ago is not missed.
            until = self.server_time() + timedelta(days=1)
            want_symbol = self.symbol_for(instrument)
            deals = self.mt5.history_deals_get(since, until)
            if deals is None:
                return None
            entry_in = getattr(self.mt5, "DEAL_ENTRY_IN", 0)
            buy = getattr(self.mt5, "DEAL_TYPE_BUY", 0)
            sell = getattr(self.mt5, "DEAL_TYPE_SELL", 1)
            n = 0
            for d in deals:
                # STRICT on every field. An earlier version defaulted a missing
                # `entry` to "this is an opening", which counts anything the
                # client hands back that lacks the attribute. Over-counting is
                # the dangerous direction here: the caller takes the LARGER of
                # this and the history replay, so an over-count silently
                # blocks a session that still had allowance, while an
                # under-count is covered by the replay.
                if not hasattr(d, "entry") or not hasattr(d, "magic"):
                    continue
                if int(d.magic) != int(magic):
                    continue
                # Scoped to the instrument's OWN terminal symbol. Without
                # this a session on ES would count gold's deals against its
                # own cap, because every session shares one account.
                if str(getattr(d, "symbol", "")) != str(want_symbol):
                    continue
                if int(d.entry) != int(entry_in):
                    continue                      # a close, or a reversal leg
                if int(getattr(d, "type", buy)) not in (int(buy), int(sell)):
                    continue                      # balance, credit, commission
                n += 1
            return n
        except Exception as exc:
            if self.log:
                self.log.warn(f"Could not read MT5 trade history ({exc!r}).")
            return None

    # -- interface --------------------------------------------------------
    def ask(self, instrument: str = "") -> float:
        t = self.mt5.symbol_info_tick(self.symbol_for(instrument))
        return t.ask if t else 0.0

    def bid(self, instrument: str = "") -> float:
        t = self.mt5.symbol_info_tick(self.symbol_for(instrument))
        return t.bid if t else 0.0

    def owns(self, magic) -> bool:
        """Is this one of ours? Anything else on the account is invisible."""
        try:
            return int(magic) in self.magics
        except (TypeError, ValueError):
            return False

    def _my_positions(self, instrument: str = "") -> List:
        pos = self.mt5.positions_get(symbol=self.symbol_for(instrument)) or []
        return [p for p in pos if self.owns(p.magic)]

    def positions_count(self, instrument: Optional[str] = None) -> int:
        """Open positions on this instrument. Scoped, because a gold position
        must not stop an ES session from entering."""
        if instrument is None:
            return sum(len(self._my_positions(k)) for k in self.symbols)
        return len(self._my_positions(instrument))

    def _filling_mode(self, instrument: str = ""):
        mt5 = self.mt5
        si = mt5.symbol_info(self.symbol_for(instrument))
        modes = si.filling_mode if si else 0
        if modes & 1:      # SYMBOL_FILLING_FOK
            return mt5.ORDER_FILLING_FOK
        if modes & 2:      # SYMBOL_FILLING_IOC
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def open_market(self, is_buy: bool, lots: float, sl: float, comment: str,
                    magic: int = 0, instrument: str = "",
                    price: Optional[float] = None):
        """Send a market order.

        `price` — the level a pullback entry was waiting for — is accepted and
        deliberately NOT used as the fill price. A live broker fills at ITS
        quote, not at a level we name; sending anything else would be inventing
        a price the market never offered.

        This is the same, already-documented relationship the breakout entry
        has: the simulated broker fills from the bar it is fed, the live broker
        fills at the quote it can actually get. The STRATEGY is identical in
        both — it decides on the same touch, at the same instant — and only the
        fill differs, which is the one difference the two paths are allowed to
        have.

        What that costs in practice: the EA sees the touch when the base bar
        carrying it arrives, so a live pullback fill lands at the price then
        current rather than exactly on the level. Expect live entries to sit a
        little the wrong side of the backtest's, and size that into any
        expectation built from a pullback backtest.
        """
        mt5 = self.mt5
        magic = int(magic) if magic else self.magic
        if price is not None and self.log:
            self.log.debug(f"Pullback level {price} requested; filling at the "
                           f"live quote, as a market order must.")
        price = self.ask(instrument) if is_buy else self.bid(instrument)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol_for(instrument),
            "volume": float(lots),
            "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": float(sl),
            "tp": 0.0,
            "deviation": int(self.cfg.deviation_points),
            "magic": magic,
            # hard cap at the MT5 boundary: the strategy already builds a
            # comment that fits, but a value set straight on the config must
            # not be able to get an order rejected for length
            "comment": str(comment)[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(instrument),
        }
        if self.cfg.dry_run:
            if self.log:
                self.log.info(f"DRY RUN — would send: {request}")
            return False, None, "dry run"

        result = mt5.order_send(request)
        if result is None:
            return False, None, f"order_send returned None: {mt5.last_error()}"
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return False, None, f"{result.retcode} {result.comment}"

        pos = self._find_position(result.order)
        if pos is None:
            return True, None, "position opened but could not be selected"
        return True, pos, ""

    def _find_position(self, order_ticket: int) -> Optional[Position]:
        """MQL5 FindMyPosition(): hedging -> ticket == order ticket,
        netting -> newest position of ours."""
        mine = self._my_positions()
        chosen = None
        for p in mine:
            if p.ticket == order_ticket:
                chosen = p
                break
        if chosen is None and mine:
            chosen = max(mine, key=lambda p: p.time)
        if chosen is None:
            return None
        return Position(
            ticket=chosen.ticket,
            is_buy=(chosen.type == self.mt5.POSITION_TYPE_BUY),
            lots=chosen.volume,
            entry_price=chosen.price_open,
            # naive UTC, matching every other timestamp in the system
            entry_time=datetime.fromtimestamp(
                chosen.time, tz=timezone.utc).replace(tzinfo=None),
            sl=chosen.sl, tp=chosen.tp,
            comment=chosen.comment, magic=chosen.magic,
        )

    def modify(self, position: Position, sl: float, tp: float):
        mt5 = self.mt5
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": self.symbol,
            "position": int(position.ticket),
            "sl": float(sl),
            "tp": float(tp),
        }
        result = mt5.order_send(request)
        if result is None:
            return False, f"order_send returned None: {mt5.last_error()}"
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return False, f"{result.retcode} {result.comment}"
        position.sl, position.tp = sl, tp
        return True, ""

    def close_all(self, reason: str) -> None:
        mt5 = self.mt5
        for p in self._my_positions():
            is_buy = p.type == mt5.POSITION_TYPE_BUY
            price = self.bid() if is_buy else self.ask()
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": self.symbol,
                "volume": p.volume,
                "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
                "position": p.ticket,
                "price": price,
                "deviation": int(self.cfg.deviation_points),
                "magic": int(p.magic),
                "comment": f"close: {reason}"[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._filling_mode(),
            }
            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                msg = mt5.last_error() if result is None else \
                    f"{result.retcode} {result.comment}"
                if self.log:
                    self.log.error(f"Close failed on #{p.ticket}: {msg}")
            elif self.log:
                self.log.info(f"Closed #{p.ticket} | reason: {reason}")

    def shutdown(self) -> None:
        try:
            self.mt5.shutdown()
        except Exception:
            pass
