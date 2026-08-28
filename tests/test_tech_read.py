"""Tests for the per-stock technical read (app/engines/tech_read.py) and its
two bulk store readers.

Pure: synthetic 1-min snapshot rows in -> pseudo-bars -> technical_read dict.
The read is what the auto-trader's confirmation gates (VWAP side, EMA trend,
break-and-hold, R:R) judge entries on, so the edges matter more than the
happy path: warm-up windows must read as None (fail-closed downstream), a
single poke through a level must NOT count as a break, and a series that
starts mid-day must not mint a fake opening range.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.engines import tech_read as TR
from app.engines.tech_read import snapshot_rows_to_bars, technical_read

D = date(2026, 8, 27)
OPEN = datetime(2026, 8, 27, 9, 15)


def _rows(prices, start=OPEN, vol_step=10_000, cum0=100_000):
    """1-min snapshot rows: (ts, fut_ltp, cumulative_volume)."""
    out = []
    cum = cum0
    for i, p in enumerate(prices):
        cum += vol_step
        out.append((start + timedelta(minutes=i), p, cum))
    return out


PREV = {"high": 110.0, "low": 100.0, "close": 108.0, "date": "2026-08-26"}
# floor pivots from PREV: p=106, r1=112, s1=102, r2=116, s2=96, r3=122, s3=92


# --- bar synthesis ----------------------------------------------------------

def test_snapshot_rows_to_bars_deltas_and_gaps():
    t0 = OPEN
    rows = [(t0, 100.0, 1000.0),
            (t0 + timedelta(minutes=1), None, 2000.0),   # no ltp -> skipped
            (t0 + timedelta(minutes=2), 101.0, 3500.0),
            (t0 + timedelta(minutes=3), 102.0, None),    # no volume -> 0
            (t0 + timedelta(minutes=4), 103.0, 3000.0),  # counter went BACK
            (t0 + timedelta(minutes=5), 0.0, 4000.0)]    # zero ltp -> skipped
    bars = snapshot_rows_to_bars(rows)
    assert [b.close for b in bars] == [100.0, 101.0, 102.0, 103.0]
    assert bars[0].volume == 0.0                 # first row has no baseline
    # the skipped-ltp row still advanced the cum baseline: 3500-2000, not -1000
    assert bars[1].volume == 1500.0
    assert bars[2].volume == 0.0                 # None volume
    assert bars[3].volume == 0.0                 # decreasing counter clamped
    assert all(b.open == b.high == b.low == b.close for b in bars)


def test_empty_series_is_all_none():
    t = technical_read([], PREV)
    assert t["n_bars"] == 0 and t["ltp"] is None
    assert t["vwap"] is None and t["trend"] is None and t["rr_ce"] is None


# --- warm-up: None until the indicators have real history -------------------

def test_warmup_leaves_trend_and_rsi_none():
    bars = snapshot_rows_to_bars(_rows([100 + i * 0.1 for i in range(10)]))
    t = technical_read(bars, PREV)
    assert t["n_bars"] == 10
    assert t["trend"] is None                    # EMA21 needs 21 bars
    assert t["rsi"] is None                      # RSI14 needs >14 bars
    assert t["vwap"] is not None                 # VWAP is fine from bar one


# --- VWAP side + distance ---------------------------------------------------

def test_vwap_side_and_distance_sign():
    # steadily rising: last price sits ABOVE the session VWAP
    up = technical_read(
        snapshot_rows_to_bars(_rows([100 + i * 0.2 for i in range(40)])), PREV)
    assert up["vwap_dist_pct"] > 0
    assert up["trend"] == "up"
    down = technical_read(
        snapshot_rows_to_bars(_rows([108 - i * 0.2 for i in range(40)])), PREV)
    assert down["vwap_dist_pct"] < 0
    assert down["trend"] == "down"
    assert down["rsi"] < 50 < up["rsi"]


# --- opening range + the restart guard --------------------------------------

def test_opening_range_from_session_open():
    prices = [100, 101, 100.5, 99.5, 100.2] + [100.4] * 30
    t = technical_read(snapshot_rows_to_bars(_rows(prices)), PREV)
    # first 15 minutes = first 15 bars (all within 100 +/- 1)
    assert t["or_high"] == 101.0
    assert t["or_low"] == 99.5


def test_midday_series_has_no_opening_range():
    """A series that starts at 11:00 (scanner restart / first recorded day)
    must NOT mint a fake 'opening' range from whenever recording began."""
    start = datetime(2026, 8, 27, 11, 0)
    t = technical_read(
        snapshot_rows_to_bars(_rows([100 + i * 0.1 for i in range(40)],
                                    start=start)), PREV)
    assert t["or_high"] is None and t["or_low"] is None


def test_too_young_session_has_no_opening_range_yet():
    # 09:15 + only 10 bars: the 15-min window hasn't elapsed
    t = technical_read(
        snapshot_rows_to_bars(_rows([100 + i * 0.1 for i in range(10)])), PREV)
    assert t["or_high"] is None


# --- break-and-hold ---------------------------------------------------------

def test_break_needs_a_hold_not_a_poke():
    # mid-day series (no opening range) so prev-day high 110 is the only
    # up-level in play
    start = datetime(2026, 8, 27, 11, 0)
    base = [104.0 + (i % 3) * 0.2 for i in range(30)]
    # single poke above 110, then back below -> NOT a break
    t = technical_read(
        snapshot_rows_to_bars(_rows(base + [110.5, 109.0, 108.5],
                                    start=start)), PREV)
    assert t["structure_break"] is None
    # three consecutive closes above -> confirmed break, level recorded
    t2 = technical_read(
        snapshot_rows_to_bars(_rows(base + [110.5, 110.8, 111.0],
                                    start=start)), PREV)
    assert t2["structure_break"] == "up"
    assert t2["break_level"] == 110.0


def test_break_down_mirrors():
    base = [104.0] * 30
    t = technical_read(
        snapshot_rows_to_bars(_rows(base + [99.5, 99.2, 99.0])), PREV)
    assert t["structure_break"] == "down"
    assert t["break_level"] == 100.0             # prev-day low


def test_no_prev_levels_still_breaks_on_opening_range():
    prices = [100, 101, 100.5, 99.5, 100.2] + [100.4] * 22 + [101.2, 101.4, 101.5]
    t = technical_read(snapshot_rows_to_bars(_rows(prices)), None)
    assert t["prev_close"] is None and t["pivots"] is None
    assert t["structure_break"] == "up"          # broke OR high 101
    assert t["break_level"] == 101.0
    assert t["rr_ce"] is None                    # no pivots -> no R:R


# --- structural risk:reward -------------------------------------------------

def test_rr_target_is_next_pivot_and_stop_is_break_level():
    # rise from 104 and hold above prev-day high 110; ltp 111
    prices = [104.0] * 30 + [110.5, 110.8, 111.0]
    t = technical_read(snapshot_rows_to_bars(_rows(prices)), PREV)
    assert t["target_ce"] == 112.0               # r1, first pivot above 111
    assert t["stop_ce"] == 110.0                 # break level beats lower VWAP
    assert t["rr_ce"] == pytest.approx((112 - 111) / (111 - 110), abs=0.01)


def test_rr_skips_to_next_level_when_price_is_beyond_r1():
    prices = [104.0] * 30 + [112.5, 112.8, 113.0]     # above r1=112
    t = technical_read(snapshot_rows_to_bars(_rows(prices)), PREV)
    assert t["target_ce"] == 116.0               # r2
    assert t["structure_break"] == "up" and t["stop_ce"] == 110.0
    assert t["rr_ce"] == pytest.approx((116 - 113) / (113 - 110), abs=0.01)


def test_rr_pe_mirror():
    # fall from 104 and hold below prev-day low 100; ltp 99
    prices = [104.0] * 30 + [99.5, 99.2, 99.0]
    t = technical_read(snapshot_rows_to_bars(_rows(prices)), PREV)
    assert t["target_pe"] == 96.0                # s2 (s1=102 is above ltp)
    assert t["stop_pe"] == 100.0                 # break level beats higher VWAP
    assert t["rr_pe"] == pytest.approx((99 - 96) / (100 - 99), abs=0.01)


# --- store: the two bulk readers -------------------------------------------

from app.data.store import DataStore  # noqa: E402


def _snap(sym, ts, ltp, vol, hi=None, lo=None):
    return {"symbol": sym, "ts": ts, "spot": None, "fut_ltp": ltp,
            "day_open": None, "day_high": hi if hi is not None else ltp,
            "day_low": lo if lo is not None else ltp,
            "prev_close": None, "volume": vol, "oi": 1000.0}


def _mk_store(tmp_path) -> DataStore:
    st = DataStore(tmp_path / "t.duckdb")
    rows = []
    # prev session (Aug 26): running extremes end at H=110/L=100, close 108
    prev = datetime(2026, 8, 26, 9, 15)
    for i, (px, hi, lo) in enumerate([(101, 103, 100), (105, 107, 100),
                                      (108, 110, 100)]):
        rows.append(_snap("RELIANCE", prev + timedelta(minutes=i), px,
                          1000 * (i + 1), hi=hi, lo=lo))
    # today (Aug 27): three rows ascending
    for i, px in enumerate([104.0, 104.5, 105.0]):
        rows.append(_snap("RELIANCE", OPEN + timedelta(minutes=i), px,
                          5000 * (i + 1)))
        rows.append(_snap("TCS", OPEN + timedelta(minutes=i), 3000.0 + i,
                          2000 * (i + 1)))
    # STALE traded only 10 days ago — outside the 7-day prev-day lookback
    rows.append(_snap("STALE", datetime(2026, 8, 17, 9, 15), 50.0, 100))
    st.upsert_stock_snapshots(rows)
    return st


def test_stock_day_series_bulk(tmp_path):
    st = _mk_store(tmp_path)
    out = st.stock_day_series_bulk(["RELIANCE", "TCS", "ABSENT"], D)
    assert set(out) == {"RELIANCE", "TCS"}       # absent symbol omitted
    rel = out["RELIANCE"]
    assert [r[1] for r in rel] == [104.0, 104.5, 105.0]     # ascending
    assert rel[0][0] < rel[1][0] < rel[2][0]
    assert rel[-1][2] == 15000                   # cumulative volume rides along
    # only today's rows — the prev session's are excluded
    assert len(rel) == 3
    assert st.stock_day_series_bulk([], D) == {}


def test_stock_prev_day_levels_bulk(tmp_path):
    st = _mk_store(tmp_path)
    out = st.stock_prev_day_levels_bulk(["RELIANCE", "TCS", "STALE"], D)
    # RELIANCE: last row of Aug 26 carries the full-day running extremes
    lv = out["RELIANCE"]
    assert lv["high"] == 110 and lv["low"] == 100 and lv["close"] == 108
    assert lv["date"] == "2026-08-26"
    # TCS traded today for the first time -> no prior session
    assert "TCS" not in out
    # STALE's only session is outside the 7-day lookback bound
    assert "STALE" not in out
    assert st.stock_prev_day_levels_bulk([], D) == {}
