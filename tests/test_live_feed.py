"""The live feed must never trade a bar it cannot identify.

`stype_in="parent"` with `symbols="GC.FUT"` streams every instrument under that
parent at once — ~35 outright contracts AND every calendar spread. A spread
quotes a price DIFFERENCE and is routinely negative.

`orb/data/dbn.py` has always filtered this for backtests. `orb/data/live.py`
did not, and a real live journal showed the consequence: three consecutive
"breakouts" at 4452.900, -104.100 and 4417.300, all pushed into one resampler
as though they were one instrument. Only `dry_run: true` kept orders off a
calendar spread.

Every test here replays that shape.

    python -m pytest tests/test_live_feed.py -q
"""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest                                                    # noqa: E402

from orb.data.live import SANITY_BAND, DatabentoLiveFeed         # noqa: E402
from orb.timeutils import ServerClock                            # noqa: E402

TS = int(datetime(2026, 8, 17, 22, 39, tzinfo=timezone.utc).timestamp() * 1e9)


class Bar:
    """An OHLCVMsg as the databento client hands it over."""

    def __init__(self, instrument_id, close, ts=TS):
        self.instrument_id = instrument_id
        self.open = self.high = self.low = self.close = close
        self.volume = 10
        self.ts_event = ts


class Mapping:
    """A SymbolMappingMsg — the only thing that names an instrument_id."""

    def __init__(self, instrument_id, symbol):
        self.instrument_id = instrument_id
        self.stype_out_symbol = symbol


class Cfg:
    api_key = "x"
    dataset = "GLBX.MDP3"
    symbols = "GC.FUT"
    schema = "ohlcv-1m"
    stype_in = "parent"


def make_feed(log=None):
    """A real feed, built by the real constructor.

    An earlier version of this helper hand-assembled the instance attributes.
    That is the drift trap this whole file exists to catch: the constructor grew
    a field, the double did not, and every test failed with AttributeError
    instead of testing anything. `databento` is stubbed only so the import at
    the top of `__init__` succeeds — nothing else about it is used.
    """
    stub = types.ModuleType("databento")
    stub.Live = object
    injected = "databento" not in sys.modules
    if injected:
        sys.modules["databento"] = stub
    try:
        return DatabentoLiveFeed(Cfg(), ServerClock(utc_offset_hours=0), log)
    finally:
        if injected:
            del sys.modules["databento"]


def seeded():
    """A feed that knows three instruments and is locked to the front month."""
    f = make_feed()
    for iid, sym in ((1, "GCZ5"), (2, "GCG6"), (3, "GCZ5-GCG6")):
        f._to_bar(Mapping(iid, sym))
    f.lock_contract("GCZ5", 4466.0)
    return f


# --------------------------------------------------------------------------
def test_the_exact_records_from_the_live_journal():
    """Front month kept; the spread and the other contract dropped."""
    f = seeded()
    assert f._to_bar(Bar(1, 4452.900)) is not None, "front month was rejected"
    assert f._to_bar(Bar(3, -104.100)) is None, "a calendar spread was accepted"
    assert f._to_bar(Bar(2, 4417.300)) is None, "a second contract was accepted"
    assert f._accepted == 1
    assert f._dropped["spread"] == 1
    assert f._dropped["other_contract"] == 1


def test_negative_price_never_becomes_a_bar():
    """The backstop, independent of symbol filtering: gold is never negative."""
    f = make_feed()
    f._to_bar(Mapping(9, "GCZ5"))          # an outright, correctly named
    assert f._to_bar(Bar(9, -104.100)) is None
    assert f._dropped["bad_price"] == 1


def test_spread_is_dropped_even_when_its_price_looks_plausible():
    """A positive spread must still go: it is a difference, not a level."""
    f = make_feed()
    f._to_bar(Mapping(3, "GCZ5-GCG6"))
    assert f._to_bar(Bar(3, 12.5)) is None
    assert f._dropped["spread"] == 1


def test_colon_spreads_are_dropped_too():
    """`is_spread` covers both spellings the venue uses."""
    f = make_feed()
    f._to_bar(Mapping(4, "GC:BF Z5-G6-J6"))
    assert f._to_bar(Bar(4, 3.0)) is None
    assert f._dropped["spread"] == 1


def test_unidentified_bar_is_dropped_not_traded():
    """No mapping means no way to tell a spread from the front month."""
    f = make_feed()
    assert f._to_bar(Bar(77, 4452.9)) is None
    assert f._dropped["unidentified"] == 1


def test_unidentified_is_reported_once_and_loudly():
    """It means the whole feed is unusable, so it is an error — but one line."""
    seen = []

    class Log:
        def info(self, m): pass
        def warn(self, m): pass
        def error(self, m): seen.append(m)

    f = make_feed(log=Log())
    for _ in range(5):
        f._to_bar(Bar(77, 4452.9))
    assert len(seen) == 1, "the error repeated per record"
    assert f._dropped["unidentified"] == 5, "the count must still rise"


def test_out_of_band_price_is_rejected():
    """Last backstop: a price nowhere near the last accepted one."""
    f = seeded()
    f._to_bar(Bar(1, 4452.9, ts=TS))                 # sets the reference
    far = 4452.9 * (1 + SANITY_BAND * 2)
    assert f._to_bar(Bar(1, far, ts=TS + 60 * 1_000_000_000)) is None
    assert f._dropped["out_of_band"] == 1


def test_a_violent_but_real_move_is_not_rejected():
    """The band must never reject a genuine day. 10% is a historic move."""
    f = seeded()
    minute = 60 * 1_000_000_000
    f._to_bar(Bar(1, 4452.9, ts=TS))
    assert f._to_bar(Bar(1, 4452.9 * 1.10, ts=TS + minute)) is not None
    assert f._to_bar(Bar(1, 4452.9 * 1.10 * 0.90, ts=TS + 2 * minute)) is not None


def test_reference_tracks_forward_so_a_trend_is_not_clipped():
    """Walking 5% at a time must stay accepted however far price travels."""
    f = seeded()
    price, ts = 4452.9, TS
    for _ in range(20):
        price *= 1.05
        ts += 60 * 1_000_000_000
        assert f._to_bar(Bar(1, price, ts=ts)) is not None, f"clipped at {price:,.0f}"
    assert price > 4452.9 * 2.5, "the walk did not actually go far"


def test_unlocked_feed_still_drops_spreads():
    """Contract locking is an extra filter, not the only one."""
    f = make_feed()
    f._to_bar(Mapping(1, "GCZ5"))
    f._to_bar(Mapping(3, "GCZ5-GCG6"))
    assert f._to_bar(Bar(1, 4452.9)) is not None
    assert f._to_bar(Bar(3, -104.1)) is None


def test_mapping_records_never_become_bars():
    """A SymbolMappingMsg is captured, not emitted."""
    f = make_feed()
    assert f._to_bar(Mapping(1, "GCZ5")) is None
    assert f._symbols == {1: "GCZ5"}


def test_lock_contract_is_case_and_space_insensitive():
    f = make_feed()
    f._to_bar(Mapping(1, "GCZ5"))
    f.lock_contract("  gcz5 ")
    assert f._to_bar(Bar(1, 4452.9)) is not None


def test_stale_bar_is_dropped():
    """`Resampler.push` treats an older bucket as a NEW bar and rewinds — a
    stale 03:05 arriving after 03:15 closes the M15 bar early. The backtest
    cannot hit it (sorted, de-duplicated); a live stream can."""
    f = seeded()
    later = TS + 60 * 1_000_000_000
    assert f._to_bar(Bar(1, 4452.9, ts=later)) is not None
    assert f._to_bar(Bar(1, 4453.0, ts=TS)) is None, "a stale bar got through"
    assert f._dropped["out_of_order"] == 1


def test_repeat_of_the_same_timestamp_is_dropped():
    """Equal is not 'after'. The warm-up/live seam delivers exactly this."""
    f = seeded()
    assert f._to_bar(Bar(1, 4452.9)) is not None
    assert f._to_bar(Bar(1, 4452.9)) is None
    assert f._dropped["out_of_order"] == 1


def test_warmup_boundary_is_honoured():
    """lock_contract carries the warm-up's last bar time, so live records that
    merely repeat history are ignored."""
    f = make_feed()
    f._to_bar(Mapping(1, "GCZ5"))
    from datetime import datetime as _dt, timezone as _tz
    boundary = _dt.fromtimestamp(TS / 1e9, tz=_tz.utc).replace(tzinfo=None)
    f.lock_contract("GCZ5", 4466.0, last_time=boundary)
    assert f._to_bar(Bar(1, 4452.9, ts=TS)) is None, "history was re-ingested"
    assert f._to_bar(Bar(1, 4452.9, ts=TS + 60 * 1_000_000_000)) is not None


def test_forward_series_is_never_blocked():
    f = seeded()
    ts = TS
    for _ in range(10):
        ts += 60 * 1_000_000_000
        assert f._to_bar(Bar(1, 4452.9, ts=ts)) is not None
    assert f._dropped["out_of_order"] == 0


def test_filter_report_names_what_was_dropped():
    f = seeded()
    f._to_bar(Bar(1, 4452.9))
    f._to_bar(Bar(3, -104.1))
    report = f.filter_report()
    assert "1 bars accepted" in report
    assert "spread" in report


def test_filter_report_is_clean_when_nothing_was_filtered():
    f = seeded()
    f._to_bar(Bar(1, 4452.9))
    assert "none filtered" in f.filter_report()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
