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
from datetime import datetime, timezone

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


def check_symbol(mt5, cfg):
    head(f"2. Symbol: {cfg.mt5.symbol}")
    sym = cfg.mt5.symbol
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
        cfg_vpu = cfg.symbol.value_per_price_unit
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


def check_order(mt5, cfg, si, tick, lots=None):
    """Ask the SERVER to validate the exact request the EA would send."""
    head("3. Order validation (nothing is placed)")
    sym = cfg.mt5.symbol
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


# --------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(
        description="Check the MetaTrader 5 connection the EA will use",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python tools/mt5_check.py
  python tools/mt5_check.py --symbol XAUUSD
  python tools/mt5_check.py --place-trade            # demo account only
""")
    p.add_argument("--config", "-c", default=ENGINE_CONFIG)
    p.add_argument("--symbol", default=None,
                   help="override mt5.symbol for this check")
    p.add_argument("--lots", type=float, default=None,
                   help="lot size to validate (default: the symbol minimum)")
    p.add_argument("--place-trade", action="store_true",
                   help="actually send a test order and close it (demo only)")
    p.add_argument("--force", action="store_true",
                   help="allow --place-trade on a LIVE account")
    a = p.parse_args()

    try:
        cfg = AppConfig.load(a.config)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if a.symbol:
        cfg.mt5.symbol = a.symbol

    print("=" * 60)
    print("  MetaTrader 5 connection check")
    print("=" * 60)
    print(f"  config: {a.config} | symbol: {cfg.mt5.symbol} | "
          f"translate_levels: {cfg.mt5.translate_levels}")

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
        request = check_order(mt5, cfg, si, tick, a.lots)
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
