"""The robustness panel — the part of the report that changes a decision.

The headline says what happened. These numbers say whether it is likely to
happen again, and they are the ones worth being sure about: a concentration
figure that is quietly wrong would tell you an edge is broad when it rests on
nineteen trades.

    python -m pytest tests/test_report_insights.py -q
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orb.report import robustness, _svg_monthly, _svg_rolling_r   # noqa: E402

DAY = datetime(2026, 1, 5)


def frame(pnls, r_multiples=None, start=DAY, step_days=1):
    """A trades frame with one trade per day, in order."""
    rows = []
    bal = 100_000.0
    for i, p in enumerate(pnls):
        t = start + timedelta(days=i * step_days)
        bal += p
        rows.append({
            "entry_time": t, "exit_time": t + timedelta(hours=1),
            "session": t.normalize() if hasattr(t, "normalize") else t,
            "net_profit": float(p), "balance_after": bal,
            "r_multiple": float(r_multiples[i]) if r_multiples else
            (1.0 if p > 0 else -1.0),
        })
    df = pd.DataFrame(rows)
    df["session"] = pd.to_datetime(df["session"]).dt.normalize()
    return df


# ==========================================================================
# concentration
# ==========================================================================
def test_concentration_finds_a_book_carried_by_a_few_trades():
    """The finding that matters most: 100 trades, and a handful are all of it."""
    pnls = [-100.0] * 95 + [10_000.0] * 5
    rb = robustness(frame(pnls), None)
    assert rb["trades"] == 100
    assert rb["top_k"] == 5
    assert rb["top_k_net"] == pytest.approx(50_000.0)
    # without those five the book LOSES — the point of the whole panel
    assert rb["net_ex_top_k"] == pytest.approx(-9_500.0)
    assert rb["net_ex_top_k"] < 0 < rb["net"]


def test_a_broad_book_is_not_flagged_as_concentrated():
    rb = robustness(frame([100.0] * 100), None)
    assert rb["top_k_net"] / rb["net"] < 0.10


def test_concentration_by_month_uses_the_session_date():
    """A trade opened on the last day of a month belongs to that month, by the
    session it was built in — not by when it happened to close."""
    # 31 days of January, then the big one on 1 February, then the rest of it
    pnls = [100.0] * 31 + [50_000.0] + [100.0] * 27
    rb = robustness(frame(pnls, start=datetime(2026, 1, 1)), None)
    assert rb["best_month"] == "2026-02"
    assert rb["best_month_net"] > rb["net_ex_best_month"]
    assert rb["months_total"] == 2
    assert rb["months_positive"] == 2


# ==========================================================================
# stability
# ==========================================================================
def test_stability_splits_the_sample_in_half_by_time():
    """A book that is flat then profitable must not read as uniformly good."""
    rb = robustness(frame([0.0] * 50 + [500.0] * 50), None)
    a, b = rb["halves"]
    assert a["trades"] == b["trades"] == 50
    assert a["net"] == pytest.approx(0.0)
    assert b["net"] == pytest.approx(25_000.0)
    assert b["avg_r"] > a["avg_r"]


def test_the_halves_are_chronological_not_arbitrary():
    df = frame([1.0] * 10)
    rb = robustness(df, None)
    a, b = rb["halves"]
    assert a["to"] <= b["from"]


# ==========================================================================
# confidence
# ==========================================================================
def test_confidence_reports_a_t_statistic_and_interval():
    """A big average on a tiny, noisy sample must not look certain."""
    rb = robustness(frame([100.0] * 30, r_multiples=[0.2] * 30), None)
    assert rb["sd_r"] == pytest.approx(0.0, abs=1e-9)

    noisy = [5.0, -1.0] * 40
    rb2 = robustness(frame([100.0 if r > 0 else -100.0 for r in noisy],
                           r_multiples=noisy), None)
    assert rb2["ci_lo"] < rb2["avg_r"] < rb2["ci_hi"]
    assert rb2["t_stat"] == pytest.approx(rb2["avg_r"] / rb2["se_r"])


def test_a_wide_interval_spans_zero():
    """Half big wins, half big losses, net positive by a hair — the interval
    must admit that this could be nothing."""
    r = ([3.0] * 20 + [-2.9] * 20)
    rb = robustness(frame([300.0] * 20 + [-290.0] * 20, r_multiples=r), None)
    assert rb["ci_lo"] < 0 < rb["ci_hi"]


# ==========================================================================
# endurance and headroom
# ==========================================================================
def test_endurance_measures_time_underwater_not_just_depth():
    """Depth is what most reports show; duration is what people quit during."""
    rb = robustness(frame([-100.0] * 30 + [200.0] * 30), None)
    assert rb["underwater_days"] >= 29
    assert rb["recovered"] is True


def test_a_curve_that_never_recovers_says_so():
    rb = robustness(frame([500.0] * 10 + [-100.0] * 10), None)
    assert rb["recovered"] is False


def test_breakeven_cost_is_expectancy_per_trade():
    """Costs are never applied in this report, so this is the honest way to
    state how much room the edge has."""
    rb = robustness(frame([100.0] * 50), None)
    assert rb["breakeven_cost"] == pytest.approx(100.0)


# ==========================================================================
# the charts render
# ==========================================================================
def test_the_monthly_chart_puts_zero_where_the_data_does():
    """All-positive months must not waste half the panel below the axis."""
    svg = _svg_monthly(frame([100.0] * 60), "USD")
    assert svg.startswith("<svg") and "<rect" in svg


def test_the_monthly_chart_handles_a_losing_month():
    svg = _svg_monthly(frame([-100.0] * 31 + [100.0] * 28,
                             start=datetime(2026, 1, 1)), "USD")
    assert "var(--neg-soft)" in svg and "var(--pos-soft)" in svg


def test_the_rolling_chart_refuses_a_sample_too_small_to_roll():
    """Better to say so than to draw a line from four points."""
    out = _svg_rolling_r(frame([100.0] * 10), window=50)
    assert "<svg" not in out and "Not enough trades" in out


def test_the_rolling_chart_labels_its_axis():
    """A line with no scale shows a shape but no magnitude."""
    svg = _svg_rolling_r(frame([100.0 if i % 3 else -100.0 for i in range(200)]),
                         window=50)
    assert svg.startswith("<svg")
    assert "R</text>" in svg, "the y-axis carries no values"
    assert "whole-sample average" in svg


def test_an_empty_book_does_not_crash_anything():
    empty = pd.DataFrame()
    assert robustness(empty, None) == {}
    assert "No data" in _svg_monthly(empty, "USD")
    assert "No data" in _svg_rolling_r(empty)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
