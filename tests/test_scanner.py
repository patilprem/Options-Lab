"""Offline tests for the Tier-1 FNO stock scanner (F2).

Exercises the pure classification/parse helpers and the store round-trip
(snapshots + volume baseline) with no network or credentials. Replays a saved
batched-quote response (tests/fixtures/stock_quotes_sample.json).

Run: venv/Scripts/python -m pytest tests/test_scanner.py -q
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

from app.data.store import SCHEMA, DataStore
from app.engines import scanner

FIX = Path(__file__).parent / "fixtures"

UNIVERSE = {
    "RELIANCE": {"symbol": "RELIANCE", "spot_security_id": 2885,
                 "future_security_id": 46376, "fno_segment": "NSE_FNO",
                 "lot_size": 500},
    "INFY": {"symbol": "INFY", "spot_security_id": 1594,
             "future_security_id": 46801, "fno_segment": "NSE_FNO",
             "lot_size": 400},
}


def _quotes():
    d = json.loads((FIX / "stock_quotes_sample.json").read_text())
    d.pop("_comment", None)
    return d


def _mem_store():
    con = duckdb.connect(":memory:")
    con.execute(SCHEMA)
    store = DataStore.__new__(DataStore)
    store.con = con
    store._lock = threading.Lock()
    return store


# --- pure classification ----------------------------------------------------

def test_classify_buildup_four_regimes():
    assert scanner.classify_buildup(1.0, 2.0) == "long_buildup"
    assert scanner.classify_buildup(-1.0, 2.0) == "short_buildup"
    assert scanner.classify_buildup(1.0, -2.0) == "short_covering"
    assert scanner.classify_buildup(-1.0, -2.0) == "long_unwinding"


def test_classify_buildup_edges():
    assert scanner.classify_buildup(None, 2.0) == "unknown"
    assert scanner.classify_buildup(0.0, 0.0) == "neutral"
    assert scanner.classify_buildup(0.05, 2.0, flat_eps=0.1) == "neutral"


def test_volume_surge():
    assert scanner.volume_surge(200, 100) == 2.0
    assert scanner.volume_surge(200, 0) is None
    assert scanner.volume_surge(None, 100) is None


# --- quote parsing ----------------------------------------------------------

def test_parse_stock_quote_rows():
    ts = datetime(2026, 7, 16, 10, 0, 0)
    snaps = {s["symbol"]: s for s in
             scanner.parse_stock_quote_rows(UNIVERSE, _quotes(), ts)}
    assert set(snaps) == {"RELIANCE", "INFY"}
    r = snaps["RELIANCE"]
    assert r["fut_ltp"] == 1275.0          # from the FUTURE node
    assert r["oi"] == 12000000
    assert r["prev_close"] == 1250.0
    assert r["spot"] == 1274.6             # from the CASH-EQUITY node
    assert r["day_high"] == 1280.0


def test_compute_metrics_buildup_and_range():
    ts = datetime(2026, 7, 16, 10, 0, 0)
    snaps = {s["symbol"]: s for s in
             scanner.parse_stock_quote_rows(UNIVERSE, _quotes(), ts)}
    # RELIANCE up 2% on OI up vs a lower day-open OI -> long buildup, near high.
    m = scanner.compute_metrics(snaps["RELIANCE"], day_open_oi=11_000_000,
                                day_open_price=1250.0, vol_baseline=4_000_000)
    assert m["buildup"] == "long_buildup"
    assert round(m["price_change_pct"], 2) == 2.0
    assert m["volume_surge"] == 8_200_000 / 4_000_000
    assert round(m["range_pos"], 3) == round((1275 - 1248) / (1280 - 1248), 3)
    # INFY down on OI up -> short buildup.
    m2 = scanner.compute_metrics(snaps["INFY"], day_open_oi=5_000_000,
                                 day_open_price=1580.0, vol_baseline=None)
    assert m2["buildup"] == "short_buildup"
    assert m2["volume_surge"] is None


# --- store round-trip -------------------------------------------------------

def test_snapshot_upsert_and_day_open_oi():
    store = _mem_store()
    ts0 = datetime(2026, 7, 16, 9, 20, 0)
    ts1 = datetime(2026, 7, 16, 10, 0, 0)
    store.upsert_stock_snapshots(
        scanner.parse_stock_quote_rows(UNIVERSE, _quotes(), ts0))
    # a later snap with different OI to prove first() picks the 9:20 baseline
    later = scanner.parse_stock_quote_rows(UNIVERSE, _quotes(), ts1)
    for s in later:
        if s["symbol"] == "RELIANCE":
            s["oi"] = 12_500_000
    store.upsert_stock_snapshots(later)

    open_oi, open_px = store.stock_day_open_oi("RELIANCE", date(2026, 7, 16))
    assert open_oi == 12_000_000          # the 9:20 value, not 12.5M
    latest = {r["symbol"]: r for r in store.latest_stock_snapshots()}
    assert latest["RELIANCE"]["oi"] == 12_500_000   # last() = 10:00 value


def test_volume_baseline_over_prior_days():
    store = _mem_store()
    # three prior sessions with a 10:00 snapshot volume of 1M/2M/3M, plus today.
    for i, vol in enumerate([1_000_000, 2_000_000, 3_000_000]):
        d = date(2026, 7, 13) + timedelta(days=i)   # Mon..Wed
        store.upsert_stock_snapshots([{
            "symbol": "RELIANCE", "ts": datetime(d.year, d.month, d.day, 10, 0),
            "spot": None, "fut_ltp": 1200, "day_open": 1200, "day_high": 1210,
            "day_low": 1195, "prev_close": 1198, "volume": vol, "oi": 1000}])
    today = date(2026, 7, 16)
    ref_tod = 10 * 3600
    base = store.stock_volume_baseline("RELIANCE", ref_tod, today, days=10)
    assert base == (1_000_000 + 2_000_000 + 3_000_000) / 3
