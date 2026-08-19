#!/usr/bin/env python3
"""Check the MetaTrader 5 connection before trusting it with real orders.

    python tools/mt5_check.py                    # read-only checks
    python tools/mt5_check.py --place-trade      # send a real test order (demo)

Two levels, in increasing order of commitment:

  1. READ-ONLY (default)
     Connects, reads the account and the symbol, prices a tick, and asks MT5 to
     VALIDATE a real order with `order_check()` — the same request the EA would
     send, checked by the server, without anything being placed. This catches
     almost everything: wrong symbol, AutoTrading off, unsupported filling mode,
     stop level too tight, not enough margin.

  2. --place-trade
     Actually sends a minimum-size market order, verifies the stop loss arrived,
     modifies the take profit, then closes it — the full round trip the EA
     performs. Refuses to run on a live account unless you add --force.

Nothing here uses the strategy. It exercises the same `MT5Broker` calls the EA
makes, so a pass means the plumbing the EA depends on actually works.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orb.config import AppConfig, ENV_FILE, missing_secrets                              # noqa: E402

#: the default configuration — the orb engine's own
#: master config. There is no parent config file any more.
ENGINE_CONFIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "orb", "engines", "orb", "config.yaml")

PASS, FAIL, WARN = [], [], []


def ok(label, detail=""):
    PASS.append(label)
    print(f"  [ OK ]  {label}" + (f"  —  {detail}" if detail else ""))


def bad(label, detail=""):
    FAIL.append(label)
    print(f"  [FAIL]  {label}" + (f"  —  {detail}" if detail else ""))


def warn(label, detail=""):
    WARN.append(label)
    print(f"  [WARN]  {label}" + (f"  —  {detail}" if detail else ""))


def head(title):
    print(f"\n{title}\n" + "-" * max(len(title), 58))


# --------------------------------------------------------------------------
def check_connection(mt5, cfg):
    head("1. Terminal and account")
    kwargs = {}
    if cfg.mt5.terminal_path:
        kwargs["path"] = cfg.mt5.terminal_path
    if cfg.mt5.login:
        kwargs.update(login=int(cfg.mt5.login), password=cfg.mt5.password,
                      server=cfg.mt5.server)
    if not mt5.initialize(**kwargs):
        bad("initialize()", f"{mt5.last_error()}")
        print("\n  The terminal must be RUNNING and logged in before this script "
              "can attach to it.")
        return None, None
    ok("initialize()", "attached to the terminal")

    term = mt5.terminal_info()
    if term is None:
        bad("terminal_info()", "returned None")
    else:
        ok("terminal", f"build {term.build} | {term.name}")
        if not term.connected:
            bad("terminal is connected to the broker", "no server connection")
        else:
            ok("terminal is connected to the broker")
        if not term.trade_allowed:
            bad("AutoTrading is enabled",
                "the AutoTrading button in the toolbar is OFF — orders will be "
                "rejected with retcode 10027")
        else:
            ok("AutoTrading is enabled")

    acc = mt5.account_info()
    if acc is None:
        bad("account_info()", "returned None — not logged in?")
        return term, None

    modes = {0: "DEMO", 1: "CONTEST", 2: "REAL"}
    mode = modes.get(getattr(acc, "trade_mode", None), "UNKNOWN")
    ok("account", f"#{acc.login} on {acc.server} | {mode}")
    print(f"          balance {acc.balance:,.2f} {acc.currency} | "
          f"equity {acc.equity:,.2f} | free margin {acc.margin_free:,.2f} | "
          f"leverage 1:{acc.leverage}")
    if mode == "REAL":
        warn("this is a LIVE account", "test orders are refused unless --force")
    if not getattr(acc, "trade_allowed", True):
        bad("trading is allowed on this account",
            "the account itself is read-only (investor password?)")
    else:
        ok("trading is allowed on this account")
    return term, acc


def run_symbols(cfg):
    """Every (instrument, MT5 symbol, value_per_point) this config will trade.

    A multi-instrument run sends orders to several symbols, and a symbol that
    is missing, suffixed differently or not tradeable fails only when that
    session opens — possibly hours in. So every one of them is checked here,
    not just `mt5.symbol`.

    Falls back to the single `mt5.symbol` when no `instruments:` block exists,
    which is what every config written before multi-instrument support has.
    """
    if not getattr(cfg, "instruments", None):
        return [("", cfg.mt5.symbol, cfg.symbol.value_per_price_unit)]
    return [(name, inst.mt5 or cfg.mt5.symbol,
             inst.value_per_point or cfg.symbol.value_per_price_unit)
            for name, inst in cfg.instruments.items()]


def check_symbol(mt5, cfg):
    """Check every symbol the run will trade; return the FIRST one's details.

    The order check below prices one concrete request, and the first symbol is
    the one it uses. Everything else is still verified — a broken second symbol
    is reported here rather than at 03:00 when its session opens.
    """
    symbols = run_symbols(cfg)
    first = (None, None)
    for i, (instrument, sym, value_per_point) in enumerate(symbols):
        got = check_one_symbol(mt5, cfg, sym, instrument, value_per_point,
                               index=i + 1, total=len(symbols))
        if i == 0:
            first = got
    return first


def check_one_symbol(mt5, cfg, sym, instrument="", value_per_point=None,
                     index=1, total=1):
    label = f"2. Symbol: {sym}"
    if instrument:
        label += f"   ({instrument} -> {sym})"
    if total > 1:
        label += f"   [{index} of {total}]"
    head(label)
    if not mt5.symbol_select(sym, True):
        bad("symbol_select()", f"'{sym}' not found — check the exact spelling in "
                               f"Market Watch (brokers use suffixes like XAUUSD.m)")
        return None, None
    ok("symbol_select()", "visible in Market Watch")

    si = mt5.symbol_info(sym)
    if si is None:
        bad("symbol_info()", "returned None")
        return None, None

    trade_modes = {0: "DISABLED", 1: "LONG ONLY", 2: "SHORT ONLY",
                   3: "CLOSE ONLY", 4: "FULL"}
    tm = trade_modes.get(si.trade_mode, str(si.trade_mode))
    if si.trade_mode == 4:
        ok("symbol is fully tradeable")
    else:
        bad("symbol is fully tradeable", f"trade mode is {tm}")

    print(f"          digits {si.digits} | point {si.point} | tick size "
          f"{si.trade_tick_size} | tick value {si.trade_tick_value}")
    print(f"          volume  min {si.volume_min} | step {si.volume_step} | "
          f"max {si.volume_max}")
    print(f"          stops level {si.trade_stops_level} points | freeze level "
          f"{si.trade_freeze_level}")

    # what the EA will actually use — MT5 overrides the config at runtime
    if si.trade_tick_size:
        vpu = si.trade_tick_value / si.trade_tick_size
        cfg_vpu = (value_per_point if value_per_point
                   else cfg.symbol.value_per_price_unit)
        detail = f"MT5 says {vpu:,.2f} per 1.0 of price per lot"
        if abs(vpu - cfg_vpu) > 0.01 * max(vpu, 1):
            warn("value per price unit matches the config",
                 f"{detail}; config says {cfg_vpu:,.2f} — your backtest P&L is "
                 f"scaled differently from live")
        else:
            ok("value per price unit matches the config", detail)

    tick = mt5.symbol_info_tick(sym)
    if tick is None or not tick.ask:
        bad("live tick", "no quote — is the market open?")
        return si, None
    spread = tick.ask - tick.bid
    ok("live tick", f"bid {tick.bid} / ask {tick.ask} | spread "
                    f"{spread:.{si.digits}f} ({spread/si.point:,.0f} points)")
    age = None
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromtimestamp(tick.time, tz=timezone.utc)).total_seconds()
    except Exception:
        pass
    if age is not None and age > 120:
        warn("quote is fresh", f"last tick was {age/60:.1f} minutes ago — market "
                               f"closed, or the symbol is not streaming")
    elif age is not None:
        ok("quote is fresh", f"{age:.0f}s old")
    return si, tick


def check_order(mt5, cfg, si, tick, lots=None, sym=None):
    """Ask the SERVER to validate the exact request the EA would send.

    `sym` is the symbol the request is priced on — the first instrument's, so
    a multi-instrument run validates an order it would really send rather than
    one on a symbol none of its sessions trades.
    """
    sym = sym or run_symbols(cfg)[0][1]
    head(f"3. Order validation on {sym} (nothing is placed)")
    lot = lots or si.volume_min
    price = tick.ask

    # a stop a realistic distance away, sized like a real ORB stop
    sl_distance = max(si.trade_stops_level * si.point * 2, price * 0.005)
    sl = round(price - sl_distance, si.digits)

    modes = si.filling_mode
    filling = (mt5.ORDER_FILLING_FOK if modes & 1 else
               mt5.ORDER_FILLING_IOC if modes & 2 else
               mt5.ORDER_FILLING_RETURN)
    names = {mt5.ORDER_FILLING_FOK: "FOK", mt5.ORDER_FILLING_IOC: "IOC",
             mt5.ORDER_FILLING_RETURN: "RETURN"}
    ok("filling mode chosen", names.get(filling, str(filling)))

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": sym,
        "volume": float(lot),
        "type": mt5.ORDER_TYPE_BUY,
        "price": price,
        "sl": float(sl),
        "tp": 0.0,
        "deviation": int(cfg.mt5.deviation_points),
        "magic": int(next(iter(sorted({s.magic for s in cfg.enabled_sessions()})))),
        "comment": "mt5_check",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    print(f"          BUY {lot} {sym} @ {price} with SL {sl} "
          f"(distance {sl_distance:.{si.digits}f})")

    res = mt5.order_check(request)
    if res is None:
        bad("order_check()", f"{mt5.last_error()}")
        return request
    if res.retcode == 0:
        ok("server accepted the request", "margin and levels are valid")
        print(f"          margin required {res.margin:,.2f} | free after "
              f"{res.margin_free:,.2f} | balance {res.balance:,.2f}")
    else:
        bad("server accepted the request", f"retcode {res.retcode}: {res.comment}")
        if res.retcode == 10019:
            print("          not enough money for this lot size")
        elif res.retcode == 10016:
            print("          the stop level is too close to the market price")
        elif res.retcode == 10027:
            print("          AutoTrading is disabled in the terminal")
    return request


def place_round_trip(mt5, cfg, si, tick, request, force):
    """The full open → verify SL → set TP → close cycle, for real."""
    head("4. Live round trip")
    acc = mt5.account_info()
    if acc and getattr(acc, "trade_mode", 0) == 2 and not force:
        bad("account is a demo account",
            "this is a LIVE account — re-run with --force if you really mean it")
        return
    if cfg.mt5.dry_run:
        warn("dry_run", "config has mt5.dry_run: true, but --place-trade was "
                        "given explicitly, so the order WILL be sent")

    req = dict(request)
    req["comment"] = "mt5_check roundtrip"
    print(f"          sending: BUY {req['volume']} {req['symbol']} @ market")
    res = mt5.order_send(req)
    if res is None:
        bad("order_send()", f"{mt5.last_error()}")
        return
    if res.retcode != mt5.TRADE_RETCODE_DONE:
        bad("order accepted", f"retcode {res.retcode}: {res.comment}")
        return
    d = si.digits
    slip = res.price - req["price"]
    ok("order filled", f"ticket {res.order} @ {res.price:.{d}f} "
                       f"(slippage {slip:+.{d}f} = {slip/si.point:+,.0f} points)")

    positions = [p for p in (mt5.positions_get(symbol=req["symbol"]) or [])
                 if p.magic == req["magic"]]
    if not positions:
        bad("position visible after fill", "positions_get() returned nothing")
        return
    pos = max(positions, key=lambda p: p.time)
    ok("position found",
       f"#{pos.ticket} | {pos.volume} lots @ {pos.price_open:.{d}f}")

    if pos.sl and abs(pos.sl - req["sl"]) < si.point * 5:
        ok("stop loss arrived with the order",
           f"SL {pos.sl:.{d}f}, {abs(pos.price_open - pos.sl):.{d}f} away")
    elif pos.sl:
        warn("stop loss arrived with the order",
             f"broker set {pos.sl:.{d}f}, we asked for {req['sl']:.{d}f}")
    else:
        bad("stop loss arrived with the order",
            "no SL on the position — the EA relies on this for protection")

    # the EA sets the TP after the fill, from the real execution price
    risk = pos.price_open - pos.sl if pos.sl else (pos.price_open * 0.005)
    tp = round(pos.price_open + 4 * risk, si.digits)
    mres = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": req["symbol"],
                           "position": pos.ticket, "sl": float(pos.sl),
                           "tp": float(tp)})
    if mres is None or mres.retcode != mt5.TRADE_RETCODE_DONE:
        detail = mt5.last_error() if mres is None else f"{mres.retcode} {mres.comment}"
        bad("take profit applied after the fill", str(detail))
    else:
        ok("take profit applied after the fill",
           f"TP {tp:.{d}f}, {abs(tp - pos.price_open):.{d}f} away "
           f"(R:R 1:{abs(tp - pos.price_open)/max(risk, 1e-9):.1f})")

    tick2 = mt5.symbol_info_tick(req["symbol"])
    cres = mt5.order_send({
        "action": mt5.TRADE_ACTION_DEAL, "symbol": req["symbol"],
        "volume": pos.volume, "type": mt5.ORDER_TYPE_SELL,
        "position": pos.ticket, "price": tick2.bid,
        "deviation": int(cfg.mt5.deviation_points), "magic": req["magic"],
        "comment": "mt5_check close", "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": req["type_filling"]})
    if cres is None or cres.retcode != mt5.TRADE_RETCODE_DONE:
        detail = mt5.last_error() if cres is None else f"{cres.retcode} {cres.comment}"
        bad("position closed", f"{detail}  ***  CLOSE #{pos.ticket} BY HAND  ***")
        return
    ok("position closed", f"@ {cres.price:.{d}f}")

    still = [p for p in (mt5.positions_get(symbol=req["symbol"]) or [])
             if p.ticket == pos.ticket]
    if still:
        bad("nothing left open", f"#{pos.ticket} is still there — close it by hand")
    else:
        ok("nothing left open")
        moved = cres.price - res.price          # in price, buy then sell
        vpu = (si.trade_tick_value / si.trade_tick_size) if si.trade_tick_size else 0
        money = moved * pos.volume * vpu
        print(f"          round trip: opened {res.price:.{d}f} -> closed "
              f"{cres.price:.{d}f} = {moved:+.{d}f} "
              f"({moved/si.point:+,.0f} points)")
        print(f"          that is {money:+,.2f} on {pos.volume} lots — the real "
              f"cost of entering and leaving right now")


#: broker CFD name -> the CME futures parent symbol it usually tracks. A HINT
#: only: brokers name things freely, and only the user knows what their CFD is
#: actually priced off. Anything not listed is left blank to be filled in.
SIGNAL_HINTS = {
    "US500": "ES.FUT", "SPX500": "ES.FUT", "SP500": "ES.FUT",
    "USTEC": "NQ.FUT", "NAS100": "NQ.FUT", "NDX100": "NQ.FUT",
    "US30": "YM.FUT", "DJ30": "YM.FUT",
    "US2000": "RTY.FUT",
    "XAUUSD": "GC.FUT", "GOLD": "GC.FUT",
    "XAGUSD": "SI.FUT", "SILVER": "SI.FUT",
    "USOIL": "CL.FUT", "WTI": "CL.FUT", "CRUDE": "CL.FUT",
    "NGAS": "NG.FUT", "NATGAS": "NG.FUT",
    "COPPER": "HG.FUT",
    "EURUSD": "6E.FUT", "GBPUSD": "6B.FUT", "USDJPY": "6J.FUT",
    "AUDUSD": "6A.FUT", "USDCAD": "6C.FUT",
    "BTCUSD": "BTC.FUT", "ETHUSD": "ETH.FUT",
}


def _guess_signal(symbol: str):
    """A likely Databento parent symbol for a broker's CFD name.

    Brokers decorate symbols freely — XAUUSDm, US500.a, USTEC# — so the lookup
    strips anything that is not a letter or digit and tries progressively
    shorter prefixes. Returns `(signal, note)`; the note tells the user whether
    the value is a guess they must verify or a blank they must fill.
    """
    clean = "".join(c for c in str(symbol).upper() if c.isalnum())
    for n in range(len(clean), 2, -1):
        hit = SIGNAL_HINTS.get(clean[:n])
        if hit:
            return hit, f"GUESS from '{symbol}' — verify this is what it tracks"
    return "", "FILL THIS IN — the Databento symbol the signal comes from"


# --------------------------------------------------------------------------
def suggest_instruments(cfg, wanted) -> int:
    """Print a ready-to-paste `instruments:` entry for each symbol named.

    `value_per_point` is the one number you cannot guess reliably: it depends
    on YOUR broker's contract size for that CFD, not on the CME futures
    contract the signal comes from. Two brokers can both call a symbol US500
    and pay $50 and $1 per point respectively.

    The terminal knows it exactly:

        value_per_point = trade_tick_value / trade_tick_size

    `trade_tick_value` is what one tick is worth in your ACCOUNT currency, and
    `trade_tick_size` is how much price moves in one tick. Dividing gives the
    money per 1.0 of price, which is what the config wants.
    """
    try:
        import MetaTrader5 as mt5      # noqa: N813
    except ImportError:
        print("MetaTrader5 is not installed — this needs Windows and a running "
              "terminal.", file=sys.stderr)
        return 2

    # Deliberately NOT through `MT5Broker`: its constructor insists the
    # config's own symbol already resolves, and the whole point of this mode is
    # to look up a symbol that is not configured yet. So just attach to the
    # terminal — no strategy, no broker, nothing to trade.
    kwargs = {}
    if cfg.mt5.terminal_path:
        kwargs["path"] = cfg.mt5.terminal_path
    if cfg.mt5.login:
        kwargs.update(login=int(cfg.mt5.login), password=cfg.mt5.password,
                      server=cfg.mt5.server)
    if not mt5.initialize(**kwargs):
        print(f"MT5 initialize() failed: {mt5.last_error()}", file=sys.stderr)
        return 2

    names = [x.strip() for x in str(wanted).replace(" ", ",").split(",")
             if x.strip()]
    print("=" * 74)
    print("  Contract details read from YOUR terminal")
    print("=" * 74)

    lines, failed = [], []
    for sym in names:
        if not mt5.symbol_select(sym, True):
            failed.append((sym, "not in Market Watch — check the exact "
                                "spelling, including any broker suffix"))
            continue
        si = mt5.symbol_info(sym)
        if si is None:
            failed.append((sym, "symbol_info() returned None"))
            continue
        if not si.trade_tick_size:
            failed.append((sym, "the terminal reports no tick size"))
            continue
        vpp = si.trade_tick_value / si.trade_tick_size
        print(f"\n  {sym}")
        print(f"      contract size    {si.trade_contract_size:,.2f}")
        print(f"      tick size        {si.trade_tick_size}")
        print(f"      tick value       {si.trade_tick_value}  "
              f"(in {getattr(si, 'currency_profit', '?')})")
        print(f"      digits / point   {si.digits} / {si.point}")
        print(f"      volume min/step  {si.volume_min} / {si.volume_step}"
              + (f" / max {si.volume_max:g}"
                 if getattr(si, "volume_max", None) else ""))
        print(f"      1 lot at {si.trade_contract_size:g} x price is worth "
              f"{vpp:,.6g} per 1.0 of price")
        print(f"      -> value_per_point = {si.trade_tick_value} / "
              f"{si.trade_tick_size} = {vpp:,.6g}")
        lines.append((sym, vpp, si))

    if failed:
        print("\n  COULD NOT READ")
        for sym, why in failed:
            print(f"      {sym:<14} {why}")
        print("\n  Open Market Watch, press Ctrl+U, and copy the symbol name "
              "exactly as it\n  appears there — brokers add suffixes like "
              "'m', '.a' or '#'.")

    if lines:
        print("\n" + "=" * 74)
        print("  Paste this into the `instruments:` block of your engine "
              "config")
        print("=" * 74 + "\n")
        for sym, vpp, si in lines:
            key = "".join(c for c in sym.lower() if c.isalnum()) or "sym"
            signal, note = _guess_signal(sym)
            print(f"  {key}:")
            print(f'    signal: "{signal}"'.ljust(34) + f"# {note}")
            print(f'    mt5:    "{sym}"')
            print(f"    value_per_point: {vpp:g}")
            print(f'    data:   ["data/{key}_1m.parquet"]')
            # LOT rules are copied; PRICE rules (digits/point/tick_size) are
            # deliberately not.
            #
            # The backtest does not read the terminal — only live does — so
            # without these it happily simulates 0.10 lots on a symbol whose
            # minimum is 0.14, and live then trades 40% bigger than the study
            # said. Lot sizes are about the ORDER, so the broker's rules win.
            #
            # Rounding is about the PRICE, and the backtest prices CME futures,
            # not this CFD. XAUUSDm quotes 3 decimals where GC futures quote 2;
            # copying the 3 across shifts every stop by a tenth of a cent and
            # silently invalidates every result recorded before. Live overwrites
            # all three from MT5 at start-up anyway, so pinning them here buys
            # nothing and costs reproducibility.
            print(f"    # lot rules from your terminal, so the backtest sizes "
                  f"positions as MT5 will")
            print(f"    volume_min:  {si.volume_min:g}")
            print(f"    volume_step: {si.volume_step:g}")
            if getattr(si, "volume_max", None):
                print(f"    volume_max:  {si.volume_max:g}")
            print()
        print("  Rename the key to whatever you like — it is just the name you "
              "write on a\n  session's `instrument:` line. CHECK `signal:`: "
              "only you know which futures\n  contract your broker's CFD "
              "really tracks.\n")
        print("  Note: digits / point / tick_size are NOT included on purpose. "
              "They round\n  PRICES, and the backtest prices futures rather "
              "than this CFD — a symbol\n  quoting 3 decimals where the future "
              "quotes 2 would shift every stop.\n  Live reads them from the "
              "terminal at start-up regardless.")

    try:
        mt5.shutdown()
    except Exception:
        pass
    return 0 if lines and not failed else (1 if failed else 0)


# --------------------------------------------------------------------------
def show_deals(cfg) -> int:
    """Print what each session's magic actually opened, per session window.

    This is the same query `OrbStrategy._adopt_late_session` makes, so a figure
    in the journal can be checked against the terminal rather than trusted. The
    journal prints the count; this prints the deals behind it.
    """
    try:
        import MetaTrader5 as mt5      # noqa: N813
    except ImportError:
        print("MetaTrader5 is not installed — this needs Windows and a running "
              "terminal.", file=sys.stderr)
        return 2

    from orb.broker import MT5Broker
    magics = {s.magic for s in cfg.enabled_sessions()} or {cfg.strategy.magic}
    broker = MT5Broker(cfg.mt5, cfg.symbol, magics)
    # register the instruments so `trades_opened_since` filters on the right
    # terminal symbol — the broker maps instrument -> symbol, not the caller
    for name, inst in (cfg.instruments or {}).items():
        broker.add_instrument(name, inst.spec(cfg.symbol),
                              inst.mt5 or cfg.mt5.symbol)
    now = broker.server_time()

    symbols = {name: sym for name, sym, _v in run_symbols(cfg)}
    default_symbol = run_symbols(cfg)[0][1]

    print("=" * 74)
    print(f"  Trades opened per session — server time {now:%Y-%m-%d %H:%M}")
    print("  instruments: "
          + ", ".join(f"{name or 'default'} -> {symbol}"
                      for name, symbol in symbols.items()))
    print("=" * 74)
    for s in cfg.enabled_sessions():
        # the current instance of this session's window, in server time
        hh, mm = (int(x) for x in str(s.range_start).split(":"))
        since = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if since > now:
            since -= timedelta(days=1)
        # the session's OWN symbol — the EA counts per instrument, because
        # every session shares one account
        sym = symbols.get(getattr(s, "instrument", "") or "", default_symbol)
        n = broker.trades_opened_since(s.magic, since,
                                       instrument=getattr(s, "instrument", ""))
        cap = s.max_trades_per_session or 0
        verdict = ("could not read" if n is None else
                   f"{n} of {cap or 'unlimited'}"
                   + ("  ** AT OR OVER THE CAP **" if cap and n >= cap else ""))
        print(f"\n  {(s.name or 'MAIN'):<12} [{s.engine:<12}] magic {s.magic} "
              f"| {sym} | window opened {since:%Y-%m-%d %H:%M}")
        print(f"      counted: {verdict}")
        deals = mt5.history_deals_get(since, now + timedelta(days=1)) or []
        rows = [d for d in deals
                if getattr(d, "magic", None) == s.magic
                and getattr(d, "symbol", "") == sym]
        if not rows:
            print("      no deals")
            continue
        for d in sorted(rows, key=lambda x: x.time):
            when = datetime.fromtimestamp(d.time, tz=timezone.utc).replace(
                tzinfo=None)
            kind = "OPEN " if d.entry == mt5.DEAL_ENTRY_IN else "close"
            side = {0: "BUY ", 1: "SELL"}.get(d.type, f"t{d.type}")
            print(f"      {when:%Y-%m-%d %H:%M}  {kind} {side} "
                  f"{d.volume:>5.2f} @ {d.price:<10.3f} "
                  f"profit {d.profit:>9.2f}  #{d.position_id}")
    print("\n" + "=" * 74)
    print("  Only lines marked OPEN are counted. A 'close' is the same trade "
          "leaving.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Check the MetaTrader 5 connection the EA will use",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python tools/mt5_check.py
  python tools/mt5_check.py --suggest US500,USTEC    # get value_per_point
  python tools/mt5_check.py --symbol gc=XAUUSD       # one instrument's symbol
  python tools/mt5_check.py --deals                  # what each session opened
  python tools/mt5_check.py --place-trade            # demo account only

Every instrument declared in the config's `instruments:` block is checked, so
a wrong or missing symbol for ES shows up here rather than at 09:30 when its
session opens.
""")
    p.add_argument("--config", "-c", default=ENGINE_CONFIG)
    p.add_argument("--symbol", default=None,
                   help="override the MT5 symbol for this check. With several "
                        "instruments declared, name which one: "
                        "--symbol gc=XAUUSD")
    p.add_argument("--lots", type=float, default=None,
                   help="lot size to validate (default: the symbol minimum)")
    p.add_argument("--place-trade", action="store_true",
                   help="actually send a test order and close it (demo only)")
    p.add_argument("--force", action="store_true",
                   help="allow --place-trade on a LIVE account")
    p.add_argument("--suggest", default=None, metavar="SYM,SYM",
                   help="read the contract details of these terminal symbols "
                        "and print a ready-to-paste `instruments:` entry for "
                        "each — including the value_per_point your broker "
                        "actually uses. e.g. --suggest US500,USTEC")
    p.add_argument("--deals", action="store_true",
                   help="list the trades each session's magic has opened "
                        "today, exactly as the EA counts them, and stop. Use "
                        "this to check a 'N opened on the account' figure in "
                        "the journal against your MT5 History tab.")
    a = p.parse_args()

    try:
        cfg = AppConfig.load(a.config)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if a.symbol:
        cfg.mt5.symbol = a.symbol
        # With instruments declared, `mt5.symbol` is only a fallback — the
        # override has to reach the instrument itself or it changes nothing.
        # Ambiguous with more than one, so name which: --symbol gc=XAUUSD.
        if cfg.instruments:
            name, _, symbol = a.symbol.partition("=")
            if symbol:
                if name not in cfg.instruments:
                    print(f"Unknown instrument '{name}'. Declared: "
                          f"{', '.join(sorted(cfg.instruments))}",
                          file=sys.stderr)
                    return 2
                cfg.instruments[name].mt5 = symbol
                cfg.mt5.symbol = symbol
            elif len(cfg.instruments) == 1:
                next(iter(cfg.instruments.values())).mt5 = a.symbol
            else:
                print(f"This config declares {len(cfg.instruments)} "
                      f"instruments, so --symbol has to say which: "
                      f"--symbol {sorted(cfg.instruments)[0]}=XAUUSD",
                      file=sys.stderr)
                return 2

    # Without credentials, `MT5Broker.connect` attaches to whatever terminal is
    # already open and logged in — so every check below would pass while
    # `run_live.py` could not log in at all. Refuse instead of reporting a
    # connection the EA cannot make on its own.
    missing = missing_secrets(cfg, live=True)
    if missing:
        print(f"Missing credential(s): {', '.join(missing)}\n"
              f"They belong in {ENV_FILE} at the project root — copy "
              f".env.example to {ENV_FILE} and fill it in.\n"
              f"Without them this check would attach to whatever terminal "
              f"happens to be open and pass, while run_live.py could not log "
              f"in at all.", file=sys.stderr)
        return 2

    if a.suggest:
        return suggest_instruments(cfg, a.suggest)

    if a.deals:
        return show_deals(cfg)

    print("=" * 60)
    print("  MetaTrader 5 connection check")
    print("=" * 60)
    print(f"  config: {a.config} | translate_levels: "
          f"{cfg.mt5.translate_levels}")
    for name, sym, vpp in run_symbols(cfg):
        print(f"  {(name or 'symbol'):<10} -> {sym:<12} "
              f"{vpp:,.2f} per point per lot")

    try:
        import MetaTrader5 as mt5      # noqa: N813
    except ImportError:
        print("\n  The MetaTrader5 package is not installed.")
        print("  It only exists for Windows Python:  pip install MetaTrader5")
        return 2

    try:
        term, acc = check_connection(mt5, cfg)
        if acc is None:
            return 1
        si, tick = check_symbol(mt5, cfg)
        if si is None or tick is None:
            return 1
        request = check_order(mt5, cfg, si, tick, a.lots,
                              sym=run_symbols(cfg)[0][1])
        if a.place_trade and request is not None:
            # never send a real order once something has already failed: the
            # earlier checks exist precisely to stop a bad order being placed
            if FAIL:
                head("4. Live round trip")
                bad("earlier checks passed",
                    f"{len(FAIL)} check(s) already failed — refusing to place a "
                    f"test order until they are fixed")
            else:
                place_round_trip(mt5, cfg, si, tick, request, a.force)
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass

    print("\n" + "=" * 60)
    print(f"  {len(PASS)} passed, {len(WARN)} warning(s), {len(FAIL)} failed")
    print("=" * 60)
    if FAIL:
        print("\n  Fix the failures above before running the EA live:")
        for f in FAIL:
            print(f"    - {f}")
        return 1
    if WARN:
        print("\n  Warnings are not blocking, but read them:")
        for w in WARN:
            print(f"    - {w}")
    if not a.place_trade:
        print("\n  Nothing was placed. Add --place-trade to send a real test\n"
              "  order on a demo account and close it again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
