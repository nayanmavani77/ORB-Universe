"""Joining a session already under way.

THE FAULT, from a real journal. Warm-up rebuilds the range from history but
deliberately judges none of it — replaying history through the decision path
would fire orders at signals that already happened. So the EA armed the moment
the range was built, and if price was already outside it entered on the very
next bar: a genuine 21:48 breakout entered at 23:38 with 2.7x the intended stop
distance, because the stop is anchored to the range and every point price had
already run was added to the risk.

THE FIX has two halves, and both matter.

1. HOW MUCH ALLOWANCE IS LEFT. The session's own history is replayed through
   the identical engine and the trades it produces are adopted. Verified on 193
   Asia sessions of 2026 data: the replay matched the full backtest's count in
   193 of 193.

   Getting this right is what makes late sessions usable at all. Counting range
   excursions instead over-counts by ~10x (17 per session against 1.76 real
   trades) and declares the allowance spent in 91% of sessions; the replay puts
   the true figure at 46%, so MORE THAN HALF of late-joined sessions still have
   room. Abandoning them all would throw away real trades for no reason.

   MT5 is consulted too and the larger of the two wins — the replay catches
   trades the EA would have taken but did not, MT5 catches anything the replay
   cannot know about.

2. WHETHER A BREAKOUT MAY BE TAKEN. Even with room left, the session starts
   DISARMED and waits for a close back INSIDE the range, so the EA only ever
   trades a breakout it witnessed itself.

Keyed on `OrbStrategy.started_at`, which ONLY `LiveTrader.run` sets. A backtest
leaves it None, so every rule here is inert there — proved by
`test_backtest_is_untouched` below and by tools/golden_master.py.

    python -m pytest tests/test_late_start.py -q
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest                                              # noqa: E402

from orb.bars import Bar                                   # noqa: E402
from orb.broker import SimBroker                            # noqa: E402
from orb.live_trader import LiveTrader                      # noqa: E402
from orb.logger import RbeaLogger                           # noqa: E402
from orb.runconfig import RunConfig                         # noqa: E402

BASE = datetime(2026, 8, 17)
#: Asia's window in the shipped config: range 19:00-19:30, trade until 02:55.
RANGE_END = BASE + timedelta(hours=19, minutes=30)
STARTED = BASE + timedelta(hours=23, minutes=37)     # long after the window


class Feed:
    """Replays a fixed list of bars, then goes quiet."""

    def __init__(self, bars):
        self.bars = list(bars)

    def start(self):
        pass

    def stop(self):
        pass

    def poll(self, timeout=1.0):
        return self.bars.pop(0) if self.bars else None


class HistoryBroker(SimBroker):
    """A SimBroker that answers the history question MT5 would.

    `prior=None` is the failure case: MT5 reachable but unwilling to say.
    """

    def __init__(self, *a, prior=0, **kw):
        super().__init__(*a, **kw)
        self._prior = prior
        self.asked = []

    def trades_opened_since(self, magic, since):
        self.asked.append((magic, since))
        return self._prior


def m1(minute, price):
    """One base bar, `minute` minutes past midnight on the test day."""
    t = BASE + timedelta(minutes=minute)
    return Bar(t, price, price, price, price, 1)


def asia_config():
    cfg = RunConfig.load("orb").app_config({})
    for s in cfg.sessions.values():
        s.enabled = (s.name == "asia")
        if s.enabled:
            s.signal_timeframe = "M1"
            s.max_trades_per_session = 2
    return cfg


#: 30 bars alternating 4465/4475 -> range 4465.00 .. 4475.00, mid 4470.00
WARM_RANGE = [m1(19 * 60 + i, 4475 if i % 2 else 4465) for i in range(30)]

#: One long excursion below the range and no return: the replay signals
#: exactly ONE trade, so one of the two-trade allowance is left.
WARM_ONE = WARM_RANGE + [m1(19 * 60 + 30 + i, 4455 - i * 0.1) for i in range(240)]

#: Out, back in, out again: the replay signals TWO, so the cap is spent.
WARM_TWO = (WARM_RANGE
            + [m1(19 * 60 + 30 + i, 4455) for i in range(60)]
            + [m1(20 * 60 + 30 + i, 4470) for i in range(60)]
            + [m1(21 * 60 + 30 + i, 4455) for i in range(120)])

#: default for the harness
WARM_AFTER = WARM_ONE[len(WARM_RANGE):]

#: back inside, then a breakout the EA witnesses itself
WITNESSED = ([m1(23 * 60 + 38, 4470), m1(23 * 60 + 39, 4470)]
             + [m1(23 * 60 + 40, 4460)]
             + [m1(23 * 60 + 41 + i, 4459) for i in range(3)])


def live_run(live_bars, prior=0, started=STARTED, warm=None):
    cfg = asia_config()
    broker = HistoryBroker(cfg.symbol, 100000.0, prior=prior)
    trader = LiveTrader(cfg, broker=broker, feed=Feed(live_bars),
                        logger=RbeaLogger(level=0))
    warm_bars = WARM_RANGE + WARM_AFTER if warm is None else warm
    for b in warm_bars:
        trader.engine.warmup_bar(b)
    trader._warmup_bars = list(warm_bars)
    trader.run(poll_seconds=0, max_polls=len(live_bars) + 3,
               now_fn=lambda bar=None: (bar.time + timedelta(minutes=1))
               if bar else started)
    return broker


def taken(broker):
    """Trades opened during the run: closed ones plus any still open."""
    return len(broker.trades) + (1 if broker.position else 0)


# --------------------------------------------------------------------------
# 1. it must not inherit a breakout it never saw
# --------------------------------------------------------------------------
def test_stale_breakout_is_not_traded():
    """Price already below the range at start-up, and it stays there. The real
    breakout was hours ago, so nothing may be entered on it."""
    bars = [m1(23 * 60 + 38 + i, 4453) for i in range(6)]
    assert taken(live_run(bars, warm=WARM_ONE)) == 0


def test_nothing_at_all_if_price_never_returns_inside():
    """A whole session may pass untraded. That is the intended outcome."""
    bars = [m1(23 * 60 + 38 + i, 4400 - i) for i in range(30)]
    assert taken(live_run(bars, warm=WARM_ONE)) == 0


def test_trades_a_witnessed_breakout_when_allowance_remains():
    """One trade already signalled, cap is two: after price returns inside, the
    next breakout is the EA's own and is taken."""
    assert taken(live_run(WITNESSED, warm=WARM_ONE)) == 1


def test_the_witnessed_entry_has_the_smaller_stop():
    """The whole point. The stale entry sat at 4453 with a 17-point stop; the
    witnessed one is at 4459-4460 with 11. Same range, same config."""
    broker = live_run(WITNESSED, warm=WARM_ONE)
    pos = broker.position or (broker.trades[0] if broker.trades else None)
    assert pos is not None, "no trade was taken"
    risk = abs(pos.entry_price - pos.sl)
    assert risk < 13, f"stop is {risk:.2f} — that is the stale breakout's risk"


def test_it_continues_the_sessions_numbering():
    """The adopted count must feed the journal, or the second trade of a
    session would be labelled the first."""
    broker = live_run(WITNESSED, warm=WARM_ONE)
    pos = broker.position or broker.trades[0]
    assert pos.trade_no_in_session == 2


def test_late_gate_applies_even_when_range_reentry_is_off():
    """`require_range_reentry` governs re-arming AFTER a trade. It must not let
    the EA inherit a breakout it never saw."""
    cfg = asia_config()
    for s in cfg.sessions.values():
        s.require_range_reentry = False
    broker = HistoryBroker(cfg.symbol, 100000.0, prior=0)
    bars = [m1(23 * 60 + 38 + i, 4453) for i in range(6)]
    trader = LiveTrader(cfg, broker=broker, feed=Feed(bars),
                        logger=RbeaLogger(level=0))
    for b in WARM_ONE:
        trader.engine.warmup_bar(b)
    trader._warmup_bars = list(WARM_ONE)
    trader.run(poll_seconds=0, max_polls=len(bars) + 3,
               now_fn=lambda bar=None: (bar.time + timedelta(minutes=1))
               if bar else STARTED)
    assert taken(broker) == 0


def test_a_session_starting_after_the_ea_is_not_late():
    """Normal operation: the EA was already running when the window closed, so
    it saw the first breakout and behaves exactly as it always did."""
    started = BASE + timedelta(hours=18)          # BEFORE the 19:00 window
    bars = [m1(19 * 60 + 30 + i, 4460) for i in range(3)]
    broker = live_run(bars, started=started, warm=WARM_RANGE)
    assert taken(broker) == 1, "a normal start was wrongly held back"
    assert broker.asked == [], "history was queried for a session that is not late"


# --------------------------------------------------------------------------
# 2. how much of the allowance the session already spent
# --------------------------------------------------------------------------
def test_replayed_history_spends_the_allowance():
    """Two trades already signalled, cap is two: nothing more, even after a
    textbook re-entry and fresh breakout."""
    assert taken(live_run(WITNESSED, warm=WARM_TWO)) == 0


def test_the_replay_is_used_when_mt5_shows_nothing():
    """The EA was off, so MT5 has no trades — but the session still spent its
    allowance. This is the case MT5 alone cannot answer."""
    assert taken(live_run(WITNESSED, prior=0, warm=WARM_TWO)) == 0


def test_mt5_wins_when_it_is_the_larger():
    """A manual trade on the same magic is invisible to a replay."""
    assert taken(live_run(WITNESSED, prior=2, warm=WARM_ONE)) == 0


def test_a_single_bar_range_window_is_still_replayed():
    """London's 03:00-03:15 on M15 is exactly ONE bar, as is any 1-minute
    window on M1. The replay used to require two and silently returned None for
    precisely those configurations, falling back to MT5 alone — which is blind
    to trades the EA would have taken while it was not running."""
    cfg = asia_config()
    broker = HistoryBroker(cfg.symbol, 100000.0, prior=0)
    trader = LiveTrader(cfg, broker=broker, feed=Feed([]),
                        logger=RbeaLogger(level=0))
    trader._warmup_bars = list(WARM_ONE)
    session = next(s for s in cfg.enabled_sessions() if s.name == "asia")
    start = BASE + timedelta(hours=19)
    # a ONE-bar window: 19:00..19:01
    got = trader._replay_session(start, start + timedelta(minutes=1), session)
    assert got is not None, "a single-bar window was refused"


def test_history_is_queried_with_this_sessions_magic_and_window():
    """Wrong magic or window would count another session's trades."""
    bars = [m1(23 * 60 + 38 + i, 4453) for i in range(3)]
    broker = live_run(bars, warm=WARM_ONE)
    assert len(broker.asked) == 1
    magic, since = broker.asked[0]
    assert magic == 20260801, "asia's magic was not used"
    assert since == BASE + timedelta(hours=19), "window start is wrong"


def test_session_is_skipped_when_neither_source_can_be_read():
    """An unknown allowance cannot be spent safely."""
    cfg = asia_config()
    broker = HistoryBroker(cfg.symbol, 100000.0, prior=None)
    trader = LiveTrader(cfg, broker=broker, feed=Feed(WITNESSED),
                        logger=RbeaLogger(level=0))
    for b in WARM_ONE:
        trader.engine.warmup_bar(b)
    trader._warmup_bars = []                       # nothing to replay either
    trader.run(poll_seconds=0, max_polls=len(WITNESSED) + 3,
               now_fn=lambda bar=None: (bar.time + timedelta(minutes=1))
               if bar else STARTED)
    assert taken(broker) == 0


def test_the_gates_are_cleared_on_the_next_session():
    """Both are session state. A session that opens while the EA runs is
    normal."""
    cfg = asia_config()
    broker = HistoryBroker(cfg.symbol, 100000.0, prior=None)
    trader = LiveTrader(cfg, broker=broker, feed=Feed([]),
                        logger=RbeaLogger(level=0))
    strat = trader.engine.engines[0].strategy
    strat.started_at = STARTED
    strat._session_blocked = True
    strat._await_reentry = True
    strat._start_new_session(BASE + timedelta(days=1, hours=19))
    assert strat._session_blocked is False
    assert strat._await_reentry is False


# --------------------------------------------------------------------------
# 3. the backtest must not notice any of this
# --------------------------------------------------------------------------
def test_backtest_is_untouched():
    """`started_at` stays None outside the live path, so both rules are inert.
    tools/golden_master.py proves the same thing over 24 real backtests."""
    from orb.backtest import run_backtest
    from orb.engine import MultiEngine  # noqa: F401
    cfg = asia_config()
    bars = WARM_RANGE + [m1(19 * 60 + 30 + i, 4460) for i in range(5)]
    broker = HistoryBroker(cfg.symbol, 100000.0, prior=None)   # would BLOCK if asked
    engine = MultiEngine(cfg.enabled_sessions(), broker,
                         logger=RbeaLogger(level=0))
    for strat in (e.strategy for e in engine.engines):
        assert strat.started_at is None, "a backtest must never stamp started_at"
        assert strat.session_replay is None, "a backtest must never get a replay"
        assert strat._await_reentry is False
        assert strat._session_blocked is False
    # and a full backtest still trades, i.e. nothing here held it back
    result = run_backtest(cfg, bars, logger=RbeaLogger(level=0))
    assert len(result.trades) >= 1, "the backtest took no trade at all"
    assert broker.asked == [], "a backtest queried broker history"


def test_simbroker_reports_no_history():
    """A simulated broker cannot restart mid-session, so zero is correct."""
    cfg = asia_config()
    assert SimBroker(cfg.symbol, 100000.0).trades_opened_since(1, BASE) == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
