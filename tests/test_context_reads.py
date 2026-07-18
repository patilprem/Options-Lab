"""
Extended Context read surface (step 2)
======================================
Multi-timeframe history, ctx.chain() (chain summary incl. max_pain), and
ctx.iv_rank() — plus the pure helpers behind them (percentile_rank, max_pain,
chain_summary). Offline.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.core.contract import Bar, OptionQuote, OptionType
from app.data.store import DataStore, SyntheticStore
from app.engines import fills as F
from app.engines import indicators as ind
from app.engines import scanner
from app.engines.backtest import BacktestContext


@pytest.fixture
def store(tmp_path):
    return DataStore(tmp_path / "md.duckdb")


def _bar(ts, px=22000.0):
    return Bar(ts, px, px + 5, px - 5, px, 1000, 0)


def _q(strike, otype, oi):
    return OptionQuote(datetime(2026, 7, 16, 11, 0), "NIFTY", None, strike,
                       OptionType(otype), ltp=100.0, oi=oi, iv=15.0, volume=100)


# --- pure helpers -----------------------------------------------------------

def test_percentile_rank():
    s = [10, 20, 30, 40]
    assert ind.percentile_rank(30, s) == pytest.approx(75.0)
    assert ind.percentile_rank(40, s) == pytest.approx(100.0)
    assert ind.percentile_rank(5, s) == pytest.approx(0.0)
    assert ind.percentile_rank(None, s) is None
    assert ind.percentile_rank(10, []) is None


def test_max_pain_argmin():
    # 2000 calls @100, 1000 puts @120 -> pain min at 100
    cache = {
        ("WEEKLY", 0, 0, "CALL"): _q(100, "CALL", 2000),
        ("WEEKLY", 0, 2, "PUT"): _q(120, "PUT", 1000),
    }
    assert scanner.max_pain(cache) == pytest.approx(100)


def test_max_pain_single_strike_is_that_strike():
    cache = {
        ("WEEKLY", 0, 0, "CALL"): _q(110, "CALL", 500),
        ("WEEKLY", 0, 0, "PUT"): _q(110, "PUT", 500),
    }
    assert scanner.max_pain(cache) == pytest.approx(110)


def test_chain_summary_has_metrics_and_maxpain():
    cache = {
        ("WEEKLY", 0, 0, "CALL"): _q(22000, "CALL", 1000),
        ("WEEKLY", 0, 0, "PUT"): _q(22000, "PUT", 1500),
    }
    s = scanner.chain_summary(cache)
    assert s["pcr_oi"] == pytest.approx(1.5)
    assert "max_pain" in s and s["max_pain"] == pytest.approx(22000)


# --- BacktestContext.chain() (replay) ---------------------------------------

def _chain_row(ts, soff, otype, oi, strike):
    return ("NIFTY", ts, None, "WEEKLY", 0, strike, soff, otype, 22000.0,
            100.0, 99.5, 100.5, 15.0, oi, 500, None, None, None, None)


def test_backtest_chain_replayed(store):
    t0 = datetime(2026, 7, 16, 11, 0)
    store.upsert_chain_rows([
        _chain_row(t0, 0, "CALL", 1000, 22000),
        _chain_row(t0, 0, "PUT", 1600, 22000),
        _chain_row(t0, -2, "PUT", 400, 21900),
        _chain_row(t0, 2, "CALL", 300, 22100),
    ])
    ctx = BacktestContext("NIFTY", store, 1_000_000, F.FeeConfig(), F.SlippageConfig())
    ctx.push_bar(_bar(t0 + timedelta(minutes=2)))
    c = ctx.chain()
    assert c is not None
    assert c["pcr_oi"] == pytest.approx(2000 / 1300)
    assert c["max_pain"] is not None


def test_backtest_chain_none_without_data(store):
    ctx = BacktestContext("NIFTY", store, 1_000_000, F.FeeConfig(), F.SlippageConfig())
    ctx.push_bar(_bar(datetime(2026, 7, 16, 11, 0)))
    assert ctx.chain() is None


# --- BacktestContext.iv_rank() ----------------------------------------------

def _opt_row(ts, iv, strike=22000.0):
    # option_bars column order (15): underlying, ts, expiry_kind, expiry_offset,
    # strike_offset, option_type, strike, expiry, o, h, l, c, volume, oi, iv
    return ("NIFTY", ts, "WEEKLY", 0, 0, "CALL", strike, None,
            100.0, 100.0, 100.0, 100.0, 100, 500, iv)


def test_backtest_iv_rank(store):
    base = datetime(2026, 7, 16, 10, 0)
    # rising IV each day; latest (current) is the highest -> rank 100
    rows = [_opt_row(base + timedelta(days=i), 10.0 + i) for i in range(6)]
    with store._lock:
        store.con.executemany(
            "INSERT OR REPLACE INTO option_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows)
    ctx = BacktestContext("NIFTY", store, 1_000_000, F.FeeConfig(), F.SlippageConfig())
    ctx.push_bar(_bar(base + timedelta(days=5, minutes=30)))
    assert ctx.iv_rank(lookback_days=30) == pytest.approx(100.0)


def test_backtest_iv_rank_none_when_thin(store):
    base = datetime(2026, 7, 16, 10, 0)
    with store._lock:
        store.con.executemany(
            "INSERT OR REPLACE INTO option_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [_opt_row(base, 12.0)])          # only 1 point (<5)
    ctx = BacktestContext("NIFTY", store, 1_000_000, F.FeeConfig(), F.SlippageConfig())
    ctx.push_bar(_bar(base + timedelta(minutes=30)))
    assert ctx.iv_rank() is None


# --- multi-timeframe history ------------------------------------------------

def test_history_higher_timeframe_resampled():
    store = SyntheticStore()
    ctx = BacktestContext("NIFTY", store, 1_000_000, F.FeeConfig(), F.SlippageConfig())
    ctx._interval = 5
    now = datetime(2026, 7, 16, 12, 0)      # Thursday midday
    ctx.push_bar(_bar(now))
    htf = ctx.history(10, interval=15)
    assert htf and all(b.ts <= now for b in htf)
    # 15-min spacing between resampled bars
    if len(htf) >= 2:
        assert (htf[-1].ts - htf[-2].ts) == timedelta(minutes=15)


def test_history_own_timeframe_uses_memory():
    store = SyntheticStore()
    ctx = BacktestContext("NIFTY", store, 1_000_000, F.FeeConfig(), F.SlippageConfig())
    ctx._interval = 5
    b = _bar(datetime(2026, 7, 16, 12, 0))
    ctx.push_bar(b)
    assert ctx.history(5) == [b]
    assert ctx.history(5, interval=5) == [b]      # explicit own interval -> memory
