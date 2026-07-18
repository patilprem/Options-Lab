"""
Backtest signal replay + warmup (steps 1 & 5)
=============================================
Offline. Proves:
  * BacktestContext.signal() replays index_bias / tier2 AS-OF the bar from
    recorded data (index_bias_history, chain_snapshots), honestly returns None
    when nothing was recorded near the bar, and rejects stale carry-over reads.
  * Warmup preloads pre-`start` bars so ctx.history() is deep from the first
    on_bar, in both the backtest and paper contexts — without polluting
    now/spot/day accounting.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.contract import Bar
from app.data.store import DataStore, SyntheticStore
from app.engines import backtest as bt
from app.engines import fills as F
from app.engines.backtest import BacktestContext, _seed_warmup

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture
def store(tmp_path):
    return DataStore(tmp_path / "md.duckdb")


def _bar(ts, px=22000.0):
    return Bar(ts, px, px + 5, px - 5, px, 1000, 0)


def _ctx(store, ts):
    c = BacktestContext("NIFTY", store, 1_000_000, F.FeeConfig(), F.SlippageConfig())
    c.push_bar(_bar(ts))            # sets ctx.now = ts
    return c


# --- index_bias replay ------------------------------------------------------

def test_index_bias_replayed_asof_bar(store):
    t0 = datetime(2026, 7, 16, 10, 0)
    store.upsert_index_bias(t0, "NIFTY", {
        "score": 0.55, "buildup_breadth": 0.5, "price_breadth": 0.6,
        "bull_weight": 40.0, "bear_weight": 5.0, "coverage": 60.0, "n": 12,
        "spot": 22000.0})
    # a bar 4 minutes after the reading sees it
    ctx = _ctx(store, t0 + timedelta(minutes=4))
    sig = ctx.signal("index_bias")
    assert sig is not None
    assert sig["score"] == 0.55 and sig["label"] == "bullish"
    assert sig["coverage"] == 60.0 and sig["n"] == 12
    assert sig["replayed"] is True


def test_index_bias_none_before_first_reading(store):
    t0 = datetime(2026, 7, 16, 10, 0)
    store.upsert_index_bias(t0, "NIFTY", {"score": 0.55, "spot": 22000.0})
    # a bar BEFORE the reading must not see the future value
    ctx = _ctx(store, t0 - timedelta(minutes=5))
    assert ctx.signal("index_bias") is None


def test_index_bias_stale_reading_rejected(store):
    t0 = datetime(2026, 7, 16, 10, 0)
    store.upsert_index_bias(t0, "NIFTY", {"score": 0.55, "spot": 22000.0})
    # 40 min later, past the 20-min freshness window -> unknown, not carried
    ctx = _ctx(store, t0 + timedelta(minutes=40))
    assert ctx.signal("index_bias") is None


# --- tier2 (chain metrics) replay -------------------------------------------

def _chain_row(u, ts, soff, otype, oi, iv, strike):
    # matches chain_snapshots column order
    return (u, ts, None, "WEEKLY", 0, strike, soff, otype, 22000.0,
            100.0, 99.5, 100.5, iv, oi, 500, None, None, None, None)


def test_tier2_replayed_from_chain_snapshots(store):
    t0 = datetime(2026, 7, 16, 11, 0)
    rows = [
        _chain_row("NIFTY", t0, 0, "CALL", 1000, 14.0, 22000),
        _chain_row("NIFTY", t0, 0, "PUT", 1500, 15.0, 22000),
        _chain_row("NIFTY", t0, -1, "PUT", 800, 16.0, 21950),
        _chain_row("NIFTY", t0, 1, "CALL", 700, 13.0, 22050),
    ]
    store.upsert_chain_rows(rows)
    ctx = _ctx(store, t0 + timedelta(minutes=2))
    t2 = ctx.signal("tier2")
    assert t2 is not None
    # PCR-OI = total put OI / total call OI = 2300 / 1700
    assert t2["pcr_oi"] == pytest.approx(2300 / 1700)
    assert t2["iv_skew"] is not None      # OTM put IV - OTM call IV
    assert t2["atm_iv"] == pytest.approx((14.0 + 15.0) / 2)


def test_tier2_none_when_no_snapshot(store):
    ctx = _ctx(store, datetime(2026, 7, 16, 11, 0))
    assert ctx.signal("tier2") is None


def test_tier1_and_setup_never_replayed(store):
    t0 = datetime(2026, 7, 16, 11, 0)
    store.upsert_index_bias(t0, "NIFTY", {"score": 0.55, "spot": 22000.0})
    ctx = _ctx(store, t0 + timedelta(minutes=1))
    assert ctx.signal("tier1") is None
    assert ctx.signal("setup") is None


def test_synthetic_store_signals_all_none():
    ctx = _ctx(SyntheticStore(), datetime(2026, 7, 16, 11, 0))
    for name in ("index_bias", "tier2", "tier1", "setup"):
        assert ctx.signal(name) is None


# --- warmup (step 5) --------------------------------------------------------

def test_backtest_warmup_seeds_history():
    store = SyntheticStore()
    start = datetime(2026, 7, 16, 9, 15)
    ctx = BacktestContext("NIFTY", store, 1_000_000, F.FeeConfig(), F.SlippageConfig())
    _seed_warmup(ctx, store, "NIFTY", start, 5, 30)
    assert 0 < len(ctx._warmup) <= 30
    assert all(b.ts < start for b in ctx._warmup)
    # history() prepends warmup ahead of the live window
    ctx.push_bar(_bar(start))
    hist = ctx.history(1000)
    assert hist[-1].ts == start
    assert len(hist) == len(ctx._warmup) + 1


class _WarmupProbe(bt.Strategy):
    """Records how deep history() is on its first on_bar."""
    seen = None

    def __init__(self):
        self.params = {"warmup_bars": 40}

    def meta(self):
        from app.core.contract import StrategyMeta
        return StrategyMeta(name="probe", underlying="NIFTY", timeframe="5",
                            params=self.params)

    def on_bar(self, ctx, bar):
        if _WarmupProbe.seen is None:
            _WarmupProbe.seen = len(ctx.history(10_000))


def test_run_backtest_honors_warmup_param():
    _WarmupProbe.seen = None
    store = SyntheticStore()
    start = datetime(2026, 7, 16, 9, 15)
    end = datetime(2026, 7, 16, 15, 30)
    bt.run_backtest(_WarmupProbe(), store, start, end, 1_000_000)
    # first bar already sees warmup lookback (>1 bar), not a cold start
    assert _WarmupProbe.seen is not None and _WarmupProbe.seen > 1


def test_paper_warmup_prepends_history():
    from app.engines.paper import MarketHub, PaperContext
    from app.core import registry

    now = datetime.now(IST).replace(tzinfo=None)
    past = [_bar(now - timedelta(minutes=5 * i)) for i in range(1, 25)]
    future = [_bar(now + timedelta(minutes=5))]

    class _FakeStore:
        def underlying_bars(self, u, s, e, i):
            return [b for b in (past + future) if s <= b.ts <= e]

    rec = registry.StrategyRecord(
        id="wu", name="x", code="", state=registry.State.RUNNING,
        allocated_capital=1_000_000, square_off_on_pause=False,
        meta_json="{}", created_at="", updated_at="")
    hub = MarketHub(_FakeStore())
    ctx = PaperContext(rec, "NIFTY", hub, interval=5)
    seeded = ctx.warmup(hub.store, 20)
    assert seeded == 20
    assert all(b.ts < now for b in ctx._warmup)    # future bar excluded
    # live bar arrives; history() shows warmup THEN live, but day counters
    # (which read _bars only) stay untouched by warmup
    ctx.push_bar(future[0])
    assert ctx.history(1000)[-1].ts == future[0].ts
    assert len(ctx.history(1000)) == 21
