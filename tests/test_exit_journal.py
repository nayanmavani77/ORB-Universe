"""Every closed position must produce exactly one EXIT line.

THE FAULT, from a real journal on 2026-08-18. `_after_tick` queried a MOVING
window `[last_check, wall]` built from `datetime.now(timezone.utc)`:

  * MT5 reports deal times in the BROKER's server time, not UTC — on a server
    at UTC+3 the window sat three hours in the past, so a deal that had just
    happened fell outside it;
  * `last_check` advanced whether or not anything was found, so a deal the
    window missed was missed FOREVER.

A take-profit on ticket #4549601521 never produced an EXIT line, while a
stop-loss eight minutes later did. Trading was unaffected — `positions_count`
reads positions directly and the EA correctly re-armed once flat — but the
journal, the only record of what the account actually did, lost a trade.

Now: a wide window anchored on the broker's own clock, plus de-duplication by
deal ticket. Being early or late loses nothing; nothing is reported twice.

    python -m pytest tests/test_exit_journal.py -q
"""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest                                              # noqa: E402

from orb.broker import SimBroker                           # noqa: E402
from orb.live_trader import LiveTrader                     # noqa: E402
from orb.logger import RbeaLogger                          # noqa: E402
from orb.runconfig import RunConfig                        # noqa: E402

def in_window(session, window: str) -> bool:
    """Does this session belong to that WINDOW?

    A window trading several instruments expands to one session per cell —
    `london_gc`, `london_es` — so a test meaning "the London window" cannot
    match on the bare name any more. These tests drive gold's synthetic bars,
    so they want the gc cell.
    """
    name = session.name or ""
    return name == window or name.startswith(window + "_")
SERVER_NOW = datetime(2026, 8, 18, 9, 20)
MAGIC = 20260903


class Deal:
    def __init__(self, ticket, position_id, reason, entry, magic=MAGIC,
                 symbol="XAUUSDm", price=4390.0, profit=12.0):
        self.ticket = ticket
        self.position_id = position_id
        self.reason = reason
        self.entry = entry
        self.magic = magic
        self.symbol = symbol
        self.price = price
        self.profit = profit
        self.swap = 0.0
        self.commission = 0.0


def fake_mt5(deals):
    """Just enough of the MetaTrader5 module for the poll."""
    m = types.SimpleNamespace(
        DEAL_REASON_SL=4, DEAL_REASON_TP=5, DEAL_REASON_EXPERT=3,
        DEAL_REASON_CLIENT=0, DEAL_REASON_MOBILE=1, DEAL_REASON_WEB=2,
        DEAL_REASON_SO=6, DEAL_ENTRY_IN=0, DEAL_ENTRY_OUT=1)
    m.calls = []

    def history_deals_get(a, b):
        m.calls.append((a, b))
        return list(deals)

    m.history_deals_get = history_deals_get
    return m


class Broker(SimBroker):
    """A SimBroker wearing an mt5 module and a server clock, as MT5Broker has.

    `server_time` deliberately differs from UTC — that offset is the bug.
    """

    def __init__(self, spec, mt5):
        super().__init__(spec, 100000.0)
        self.mt5 = mt5
        self.symbol = "XAUUSDm"

    def server_time(self):
        return SERVER_NOW

    def owns(self, magic):
        return int(magic) == MAGIC


def trader_with(deals):
    cfg = RunConfig.load("orb_reverse").app_config({})
    for s in cfg.sessions.values():
        s.enabled = in_window(s, "london") and (s.instrument or "gc") == "gc"
    cfg.mt5.symbol = "XAUUSDm"

    class Feed:
        def start(self): pass
        def stop(self): pass
        def poll(self, timeout=1.0): return None

    mt5 = fake_mt5(deals)
    t = LiveTrader(cfg, broker=Broker(cfg.symbol, mt5), feed=Feed(),
                   logger=RbeaLogger(level=0))
    return t, mt5


def exits_reported(trader, monkey):
    seen = []
    for engine in trader.engine.engines:
        engine.strategy.report_exit = (
            lambda tk, how, px, net, cur, _s=seen: _s.append((tk, how)))
    return seen


def poll(trader, times=1):
    """Run the poll, defeating the throttle between calls."""
    for _ in range(times):
        trader._last_deal_check = None
        trader._after_tick(SERVER_NOW)


# --------------------------------------------------------------------------
def test_a_take_profit_is_journalled():
    """The exact exit that went missing."""
    d = Deal(1, 4549601521, reason=5, entry=1)
    t, _ = trader_with([d])
    seen = exits_reported(t, None)
    poll(t)                                    # primes on the first pass
    seen.clear()
    t._seen_deals.clear()
    poll(t)
    assert seen and seen[0][1] == "TAKE PROFIT hit", seen


def test_a_stop_loss_is_journalled():
    d = Deal(2, 4549663028, reason=4, entry=1)
    t, _ = trader_with([d])
    seen = exits_reported(t, None)
    poll(t)
    seen.clear()
    t._seen_deals.clear()
    poll(t)
    assert seen and seen[0][1] == "STOP LOSS hit", seen


def test_an_exit_is_never_reported_twice():
    """A wide window sees the same deal on every poll."""
    d = Deal(3, 999, reason=5, entry=1)
    t, _ = trader_with([d])
    seen = exits_reported(t, None)
    poll(t)                                    # prime
    seen.clear()
    t._seen_deals.clear()
    poll(t, times=6)
    assert len(seen) == 1, f"reported {len(seen)} times"


def test_deals_from_before_start_up_are_not_announced():
    """Priming: yesterday's trades must not print as if they just happened."""
    old = [Deal(10, 111, reason=5, entry=1), Deal(11, 222, reason=4, entry=1)]
    t, _ = trader_with(old)
    seen = exits_reported(t, None)
    poll(t)
    assert seen == [], "history was journalled as new"


def test_a_new_exit_after_priming_is_announced():
    deals = [Deal(10, 111, reason=5, entry=1)]
    t, _ = trader_with(deals)
    seen = exits_reported(t, None)
    poll(t)                                    # prime on the old one
    assert seen == []
    deals.append(Deal(12, 333, reason=4, entry=1))
    poll(t)
    assert len(seen) == 1 and seen[0][0] == 333, seen


def test_opens_are_not_reported_as_exits():
    """An entry deal is journalled on placement, not here."""
    t, _ = trader_with([Deal(20, 444, reason=3, entry=0)])
    seen = exits_reported(t, None)
    poll(t)
    t._seen_deals.clear()
    poll(t)
    assert seen == []


def test_another_eas_magic_is_ignored():
    t, _ = trader_with([Deal(21, 555, reason=5, entry=1, magic=12345)])
    seen = exits_reported(t, None)
    poll(t)
    t._seen_deals.clear()
    poll(t)
    assert seen == []


def test_another_symbol_is_ignored():
    t, _ = trader_with([Deal(22, 666, reason=5, entry=1, symbol="EURUSD")])
    seen = exits_reported(t, None)
    poll(t)
    t._seen_deals.clear()
    poll(t)
    assert seen == []


def test_the_window_is_anchored_on_the_brokers_clock_not_utc():
    """The root cause. A UTC-based window on a server at a different offset
    queries the wrong hours entirely."""
    t, mt5 = trader_with([])
    poll(t)
    lo, hi = mt5.calls[-1]
    assert lo <= SERVER_NOW <= hi, "the broker's own clock is outside the window"
    assert (hi - lo) >= timedelta(days=2), "the window is not wide enough"


def test_the_poll_is_throttled():
    """A wide window is cheap but not free; it must not run every tick."""
    t, mt5 = trader_with([])
    t._after_tick(SERVER_NOW)
    n = len(mt5.calls)
    for _ in range(20):
        t._after_tick(SERVER_NOW)
    assert len(mt5.calls) == n, "the poll ran on every tick"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
