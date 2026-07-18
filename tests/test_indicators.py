"""
Indicator & price-action library tests (step 3)
===============================================
Hand-verified values and structural identities for app/engines/indicators.py.
Offline, no store/network.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.core.contract import Bar
from app.engines import indicators as ind

T0 = datetime(2026, 7, 16, 9, 15)


def _bars(closes, base_ts=T0, step_min=5):
    """Bars from a close series; O=prev close, H/L bracket, vol=1000."""
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        hi = max(prev, c) + 1
        lo = min(prev, c) - 1
        out.append(Bar(base_ts + timedelta(minutes=step_min * i),
                       prev, hi, lo, c, 1000, 0))
        prev = c
    return out


def _ohlc(rows, base_ts=T0, step_min=5):
    """Bars from explicit (o,h,l,c) tuples."""
    return [Bar(base_ts + timedelta(minutes=step_min * i), o, h, l, c, 1000, 0)
            for i, (o, h, l, c) in enumerate(rows)]


# --- moving averages / momentum --------------------------------------------

def test_sma():
    assert ind.sma(_bars([1, 2, 3, 4, 5]), 3) == pytest.approx(4.0)
    assert ind.sma(_bars([1, 2]), 3) is None


def test_ema_hand_computed():
    # closes [1,2,3,4,5], n=3: seed=mean(1,2,3)=2, k=0.5 -> 3 -> 4
    assert ind.ema(_bars([1, 2, 3, 4, 5]), 3) == pytest.approx(4.0)


def test_ema_constant_series_is_constant():
    assert ind.ema(_bars([7.0] * 30), 10) == pytest.approx(7.0)


def test_rsi_extremes():
    assert ind.rsi(_bars(list(range(1, 40))), 14) == pytest.approx(100.0)   # all gains
    assert ind.rsi(_bars(list(range(40, 1, -1))), 14) == pytest.approx(0.0)  # all losses
    r = ind.rsi(_bars([1, 2, 1, 2, 1, 2, 1, 2, 1, 2, 1, 2, 1, 2, 1, 2]), 14)
    assert 0.0 < r < 100.0


def test_macd_constant_series_zero():
    m = ind.macd(_bars([100.0] * 40))
    assert m is not None
    assert m["macd"] == pytest.approx(0.0)
    assert m["hist"] == pytest.approx(0.0)


# --- volatility / trend -----------------------------------------------------

def test_true_ranges_length():
    b = _bars([1, 2, 3])
    assert len(ind.true_ranges(b)) == 2


def test_atr_constant_range():
    # every bar: high-low=10, close inside -> TR=10 -> ATR=10
    rows = [(105, 110, 100, 105)] * 20
    assert ind.atr(_ohlc(rows), 14) == pytest.approx(10.0)


def test_bollinger_constant_zero_width():
    b = ind.bollinger(_bars([50.0] * 25), 20)
    assert b["upper"] == pytest.approx(b["lower"]) == pytest.approx(50.0)
    assert b["width"] == pytest.approx(0.0)


def test_adx_trend_direction():
    up = ind.adx(_bars(list(range(1, 60))), 14)
    assert up is not None and up["plus_di"] > up["minus_di"]
    down = ind.adx(_bars(list(range(60, 1, -1))), 14)
    assert down is not None and down["minus_di"] > down["plus_di"]


def test_supertrend_follows_trend():
    up = ind.supertrend(_bars(list(range(1, 40))), period=10, mult=3.0)
    assert up["dir"] == 1 and up["level"] < 39
    down = ind.supertrend(_bars(list(range(40, 1, -1))), period=10, mult=3.0)
    assert down["dir"] == -1


def test_insufficient_data_returns_none():
    tiny = _bars([1, 2])
    assert ind.atr(tiny, 14) is None
    assert ind.supertrend(tiny) is None
    assert ind.adx(tiny) is None
    assert ind.macd(tiny) is None


# --- volume -----------------------------------------------------------------

def test_vwap_weighted():
    b0 = Bar(T0, 100, 110, 90, 105, 1, 0)          # tp = 101.667, w=1
    b1 = Bar(T0 + timedelta(minutes=5), 105, 120, 100, 110, 3, 0)  # tp=110, w=3
    tp0 = (110 + 90 + 105) / 3
    tp1 = (120 + 100 + 110) / 3
    assert ind.vwap([b0, b1]) == pytest.approx((tp0 * 1 + tp1 * 3) / 4)


def test_vwap_zero_volume_falls_back_to_typical_price():
    b0 = Bar(T0, 100, 110, 90, 100, 0, 0)
    b1 = Bar(T0 + timedelta(minutes=5), 100, 110, 90, 100, 0, 0)
    tp = (110 + 90 + 100) / 3
    assert ind.vwap([b0, b1]) == pytest.approx(tp)


# --- price-action structure -------------------------------------------------

def test_range_position():
    assert ind.range_position(Bar(T0, 105, 110, 100, 105, 0, 0)) == pytest.approx(0.5)
    assert ind.range_position(Bar(T0, 100, 110, 100, 100, 0, 0)) == pytest.approx(0.0)
    assert ind.range_position(Bar(T0, 110, 110, 100, 110, 0, 0)) == pytest.approx(1.0)
    assert ind.range_position(Bar(T0, 100, 100, 100, 100, 0, 0)) is None


def test_inside_and_outside_bar():
    inside = _ohlc([(100, 120, 80, 110), (105, 115, 90, 100)])
    assert ind.is_inside_bar(inside) is True
    assert ind.is_outside_bar(inside) is False
    outside = _ohlc([(100, 110, 95, 105), (100, 120, 90, 100)])
    assert ind.is_outside_bar(outside) is True


def test_swing_high_low():
    # peak at index 2 (high 130), trough at index 5 (low 60)
    rows = [(100, 105, 95, 100), (100, 115, 98, 110), (110, 130, 108, 120),
            (120, 122, 100, 105), (105, 108, 70, 80), (80, 85, 60, 75),
            (75, 100, 74, 95), (95, 105, 90, 100)]
    b = _ohlc(rows)
    assert ind.swing_high(b, left=2, right=2) == pytest.approx(130)
    assert ind.swing_low(b, left=2, right=2) == pytest.approx(60)


def test_break_of_structure_up():
    rows = [(100, 105, 95, 100), (100, 115, 98, 110), (110, 130, 108, 120),
            (120, 122, 110, 115), (115, 120, 112, 118), (118, 135, 116, 134)]
    assert ind.break_of_structure(_ohlc(rows), lookback=10) == "up"


# --- session references & pivots -------------------------------------------

def _two_sessions():
    d1 = _ohlc([(100, 110, 95, 105), (105, 108, 100, 102)],
               base_ts=datetime(2026, 7, 15, 9, 15))
    d2 = _ohlc([(103, 112, 101, 108), (108, 115, 107, 110)],
               base_ts=datetime(2026, 7, 16, 9, 15))
    return d1 + d2


def test_prev_day():
    pd = ind.prev_day(_two_sessions())
    assert pd["high"] == pytest.approx(110) and pd["low"] == pytest.approx(95)
    assert pd["close"] == pytest.approx(102)
    assert ind.prev_day(_ohlc([(1, 2, 0, 1)])) is None


def test_opening_range():
    day = _ohlc([(100, 110, 95, 105), (105, 108, 100, 102), (102, 120, 101, 118)],
                base_ts=datetime(2026, 7, 16, 9, 15), step_min=5)
    orr = ind.opening_range(day, minutes=10)   # first two 5-min bars
    assert orr["high"] == pytest.approx(110) and orr["low"] == pytest.approx(95)


def test_pivots_classic():
    p = ind.pivots(110, 90, 100)
    assert p["p"] == pytest.approx(100)
    assert p["r1"] == pytest.approx(110) and p["s1"] == pytest.approx(90)
    assert p["r2"] == pytest.approx(120) and p["s2"] == pytest.approx(80)
    assert p["r3"] == pytest.approx(130) and p["s3"] == pytest.approx(70)


def test_cpr_orders_tc_bc():
    c = ind.cpr(110, 90, 105)
    assert c["tc"] >= c["bc"]
    assert c["pivot"] == pytest.approx((110 + 90 + 105) / 3)


def test_pivots_from_history():
    p = ind.pivots_from_history(_two_sessions())
    assert p is not None and p["p"] == pytest.approx((110 + 95 + 102) / 3)


def test_gap_pct():
    # prior session close 102, latest session open 103 -> +0.98%
    g = ind.gap_pct(_two_sessions())
    assert g == pytest.approx(100 * (103 - 102) / 102)


def test_indicators_injected_into_loader_namespace():
    from app.core.loader import _exec_namespace
    ns = _exec_namespace()
    assert "indicators" in ns and hasattr(ns["indicators"], "ema")


def test_example_strategy_using_indicators_validates():
    """The indicators-based example must pass the loader's full pipeline
    (AST scan + compile + smoke run) — proves strategies can use the toolbox
    without imports and it survives the smoke context."""
    from pathlib import Path

    from app.core import loader
    code = Path(__file__).resolve().parents[1] / "examples" / "ema_atr_trend.py"
    r = loader.validate(code.read_text())
    assert r.ok, (r.errors, r.warnings)
    assert r.meta.params.get("warmup_bars") == 60


@pytest.mark.parametrize("fname", ["trend_rider_itm.py", "range_income_seller.py"])
def test_toolbox_example_strategies_validate(fname):
    """The trend-buyer and range-seller examples (built on indicators + the
    extended Context reads) must pass the loader pipeline and declare warmup."""
    from pathlib import Path

    from app.core import loader
    code = Path(__file__).resolve().parents[1] / "examples" / fname
    r = loader.validate(code.read_text())
    assert r.ok, (r.errors, r.warnings)
    assert r.meta.params.get("warmup_bars", 0) > 0
