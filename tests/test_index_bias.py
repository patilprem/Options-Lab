"""Offline tests for the index bias aggregation + accuracy scoring (F5)."""

from __future__ import annotations

from datetime import datetime

from app.engines import scanner


def _bull(pc=2.0):
    return {"buildup": "long_buildup", "price_change_pct": pc}


def _bear(pc=-2.0):
    return {"buildup": "short_buildup", "price_change_pct": pc}


def test_bias_bullish_when_constituents_build_longs():
    weights = {"HDFCBANK": 30.0, "ICICIBANK": 25.0, "SBIN": 10.0}
    metrics = {"HDFCBANK": _bull(), "ICICIBANK": _bull(), "SBIN": _bull()}
    b = scanner.index_bias(metrics, weights)
    assert b["score"] > 0.5
    assert b["label"] == "bullish"
    assert b["coverage"] == 65.0
    assert b["n"] == 3


def test_bias_bearish_and_signs():
    weights = {"HDFCBANK": 30.0, "ICICIBANK": 25.0}
    b = scanner.index_bias({"HDFCBANK": _bear(), "ICICIBANK": _bear()}, weights)
    assert b["score"] < -0.5 and b["label"] == "bearish"


def test_bias_weighted_and_partial_coverage():
    # heavy bull vs light bear -> net bullish; missing member drops from coverage
    weights = {"HDFCBANK": 30.0, "SBIN": 5.0, "MISSING": 40.0}
    metrics = {"HDFCBANK": _bull(), "SBIN": _bear()}
    b = scanner.index_bias(metrics, weights)
    assert b["coverage"] == 35.0            # MISSING not counted
    assert b["score"] > 0                    # HDFC weight dominates
    assert b["bull_weight"] == 30.0 and b["bear_weight"] == 5.0


def test_bias_neutral_when_empty():
    b = scanner.index_bias({}, {"HDFCBANK": 30.0})
    assert b["score"] is None and b["label"] == "neutral" and b["n"] == 0


def test_score_bias_day_hit_and_miss():
    d = datetime(2026, 7, 16, 10, 0)
    # two bullish readings at 10:00 and 10:05; spot rises 100->102 by 10:30.
    bias_rows = [(d, 0.6, 100.0),
                 (datetime(2026, 7, 16, 10, 5), 0.6, 100.5)]
    spot_bars = [(datetime(2026, 7, 16, 10, 0 + m), 100.0 + m * 0.1)
                 for m in range(0, 40, 5)]   # steadily rising
    n, hits, avg = scanner.score_bias_day(bias_rows, spot_bars, horizon_min=30)
    assert n == 2 and hits == 2 and avg > 0


def test_score_bias_day_skips_neutral():
    d = datetime(2026, 7, 16, 10, 0)
    bias_rows = [(d, 0.1, 100.0)]            # |score| < 0.3 -> not scored
    spot_bars = [(datetime(2026, 7, 16, 10, m), 100.0 + m) for m in range(0, 40, 5)]
    n, hits, avg = scanner.score_bias_day(bias_rows, spot_bars)
    assert n == 0
