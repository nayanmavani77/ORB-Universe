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
    session_start: Optional[datetime] = None
    range_high: float = 0.0
    range_low: float = 0.0
    range_mid: float = 0.0
    trade_no_in_session: int = 0
    entry_commission: float = 0.0


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

    def normalize_lot(self, lot: float) -> float:
        """MQL5 NormalizeLot()."""
        step = self.spec.volume_step or 0.01
        lot = math.floor(lot / step) * step
        lot = max(lot, self.spec.volume_min)
        lot = min(lot, self.spec.volume_max)
        return round(lot, 2)

    def normalize_price(self, price: float) -> float:
        return round(price, self.spec.digits)

    # --- required interface ---------------------------------------------
    @abstractmethod
    def ask(self) -> float: ...

    @abstractmethod
    def bid(self) -> float: ...

    @abstractmethod
    def positions_count(self) -> int: ...

    @abstractmethod
    def open_market(self, is_buy: bool, lots: float, sl: float,
                    comment: str,
                    magic: int = 0) -> Tuple[bool, Optional[Position], str]: ...

    @abstractmethod
    def modify(self, position: Position, sl: float, tp: float) -> Tuple[bool, str]: ...

    @abstractmethod
    def close_all(self, reason: str) -> None: ...

    def price_for(self, is_buy: bool) -> float:
        """Where an order would actually FILL — the execution instrument."""
        return self.ask() if is_buy else self.bid()

    def reference_price(self, is_buy: bool) -> float:
        """Where the SIGNAL was generated — the data-feed instrument.

        For a backtest, and for live trading on the same instrument the data
        came from, these are one and the same. They diverge only when the
        signal is computed on one instrument and executed on another (CME GC
        deciding, spot XAUUSD executing), which is what `translate_levels`
        exists for.
        """
        return self.price_for(is_buy)

    def trades_opened_since(self, magic: int, since: datetime):
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
        self.position: Optional[Position] = None
        self.trades: List[ClosedTrade] = []
        self.equity_curve: List[Tuple[datetime, float]] = []

        self._price = 0.0
        self._now: Optional[datetime] = None
        self.on_exit = None  # callback(ClosedTrade) — used for journal lines
        # the simulated broker executes the very instrument the bars describe,
        # so signal space and execution space are identical and levels carry
        # across unchanged
        self.translate_levels = False

    # -- market context ---------------------------------------------------
    def set_market(self, price: float, now: datetime) -> None:
        self._price = float(price)
        self._now = now

    # engine hooks
    def sync_market(self, bar: Bar, now: datetime) -> None:
        self.set_market(bar.open, now)

    def settle_bar(self, bar: Bar) -> None:
        self.process_bar(bar)

    def ask(self) -> float:
        return self._price + self.spread

    def bid(self) -> float:
        return self._price

    # -- interface --------------------------------------------------------
    def positions_count(self) -> int:
        return 1 if self.position else 0

    def open_market(self, is_buy: bool, lots: float, sl: float, comment: str,
                    magic: int = 0):
        if self.position is not None:
            return False, None, "position already open"
        price = self.price_for(is_buy)
        if price <= 0:
            return False, None, "no price"
        fill = price + (self.slippage if is_buy else -self.slippage)
        fill = self.normalize_price(fill)
        pos = Position(
            ticket=self._next_ticket, is_buy=is_buy, lots=lots,
            entry_price=fill, entry_time=self._now, sl=sl, tp=0.0,
            comment=comment, magic=int(magic),
            entry_commission=self.commission * lots,
        )
        self._next_ticket += 1
        self.position = pos
        return True, pos, ""

    def modify(self, position: Position, sl: float, tp: float):
        if self.position is None or self.position.ticket != position.ticket:
            return False, "position not found"
        self.position.sl = sl
        self.position.tp = tp
        return True, ""

    def close_all(self, reason: str) -> None:
        if self.position is None:
            return
        pos = self.position
        price = self.bid() if pos.is_buy else self.ask()
        price = price - self.slippage if pos.is_buy else price + self.slippage
        self._settle(pos, self.normalize_price(price), self._now, reason)

    # -- bar-by-bar position management ----------------------------------
    def process_bar(self, bar: Bar) -> None:
        """Walk one base bar and fire SL / TP if touched."""
        pos = self.position
        if pos is None:
            self.mark_equity(bar.time, bar.close)
            return
        if pos.entry_time is not None and bar.time < pos.entry_time:
            self.mark_equity(bar.time, bar.close)
            return

        sl_hit = tp_hit = False
        sl_price = tp_price = 0.0

        if pos.is_buy:
            # SL is below, TP is above; buys exit on the bid
            if pos.sl > 0:
                if bar.open <= pos.sl:
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
                if op_ask >= pos.sl:
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
            self._settle(pos, self.normalize_price(sl_price),
                         bar.time, "STOP LOSS hit")
        elif tp_hit:
            self._settle(pos, self.normalize_price(tp_price),
                         bar.time, "TAKE PROFIT hit")

        self.mark_equity(bar.time, bar.close)

    # -- P&L --------------------------------------------------------------
    def _settle(self, pos: Position, exit_price: float,
                exit_time: Optional[datetime], reason: str) -> None:
        direction = 1.0 if pos.is_buy else -1.0
        gross = (exit_price - pos.entry_price) * direction * pos.lots * \
            self.spec.value_per_price_unit
        commission = pos.entry_commission + self.commission * pos.lots
        net = gross - commission
        self.balance += net
        risk = abs(pos.entry_price - pos.sl) if pos.sl > 0 else 0.0
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
        )
        self.trades.append(trade)
        self.position = None
        if self.on_exit:
            self.on_exit(trade)

    def floating_pnl(self, price: float) -> float:
        if self.position is None or price is None:
            return 0.0
        d = 1.0 if self.position.is_buy else -1.0
        return (price - self.position.entry_price) * d * self.position.lots * \
            self.spec.value_per_price_unit

    def mark_equity(self, when: datetime, price: Optional[float]) -> None:
        eq = self.balance + (self.floating_pnl(price) if price is not None else 0.0)
        self.equity_curve.append((when, eq))


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
        self._feed_price = float(bar.close)

    def reference_price(self, is_buy: bool) -> float:
        """The feed's price, NOT the broker's. Falls back to the broker quote
        only if no bar has arrived yet."""
        if self._feed_price and self._feed_price > 0:
            return self._feed_price
        return self.price_for(is_buy)

    def basis(self, is_buy: bool) -> float:
        """Execution price minus feed price — how far the two instruments sit
        apart right now. Logged on every fill so a drifting basis is visible."""
        ref = self.reference_price(is_buy)
        return self.price_for(is_buy) - ref if ref else 0.0

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

    def trades_opened_since(self, magic: int, since: datetime):
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
            deals = self.mt5.history_deals_get(since, until)
            if deals is None:
                return None
            entry_in = getattr(self.mt5, "DEAL_ENTRY_IN", 0)
            return sum(
                1 for d in deals
                if int(getattr(d, "magic", -1)) == int(magic)
                and int(getattr(d, "entry", entry_in)) == int(entry_in)
                and str(getattr(d, "symbol", "")) == str(self.symbol))
        except Exception as exc:
            if self.log:
                self.log.warn(f"Could not read MT5 trade history ({exc!r}).")
            return None

    # -- interface --------------------------------------------------------
    def ask(self) -> float:
        t = self.mt5.symbol_info_tick(self.symbol)
        return t.ask if t else 0.0

    def bid(self) -> float:
        t = self.mt5.symbol_info_tick(self.symbol)
        return t.bid if t else 0.0

    def owns(self, magic) -> bool:
        """Is this one of ours? Anything else on the account is invisible."""
        try:
            return int(magic) in self.magics
        except (TypeError, ValueError):
            return False

    def _my_positions(self) -> List:
        pos = self.mt5.positions_get(symbol=self.symbol) or []
        return [p for p in pos if self.owns(p.magic)]

    def positions_count(self) -> int:
        return len(self._my_positions())

    def _filling_mode(self):
        mt5 = self.mt5
        si = mt5.symbol_info(self.symbol)
        modes = si.filling_mode if si else 0
        if modes & 1:      # SYMBOL_FILLING_FOK
            return mt5.ORDER_FILLING_FOK
        if modes & 2:      # SYMBOL_FILLING_IOC
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def open_market(self, is_buy: bool, lots: float, sl: float, comment: str,
                    magic: int = 0):
        mt5 = self.mt5
        magic = int(magic) if magic else self.magic
        price = self.ask() if is_buy else self.bid()
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
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
            "type_filling": self._filling_mode(),
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
