"""option_bars as a metrics source — the same cache shape as chain_snapshots.

Offline. chain_snapshots is only ~27 sessions deep, far too thin to validate
anything. option_bars is backfilled across hundreds of sessions and carries iv
+ oi at ATM-relative offsets, which is what atm_iv and iv_skew need.

The point of these tests is that the SAME pure functions (scanner.chain_metrics
/ max_pain) consume both sources unchanged — a historical study and a live read
must not compute their metrics two different ways.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.data.store import DataStore
from app.engines import scanner
from app.engines.event_signals import _cache_at


@pytest.fixture
def store(tmp_path):
    return DataStore(tmp_path / "md.duckdb")


def _ob_row(u, ts, soff, otype, oi, iv, strike, kind="WEEKLY", off=0):
    # matches option_bars column order
    return (u, ts, kind, off, soff, otype, strike, None,
            100.0, 101.0, 99.0, 100.0, 500, oi, iv)


def _write(store, u, ts, kind="WEEKLY", off=0, base=22000.0):
    rows = []
    for soff in range(-3, 4):
        for otype in ("CALL", "PUT"):
            iv = 15.0 + soff * 0.1 if otype == "PUT" else 14.0 - soff * 0.1
            rows.append(_ob_row(u, ts, soff, otype, 1000 + soff * 10, iv,
                                base + soff * 50, kind, off))
    store.bulk_write(
        "INSERT OR REPLACE INTO option_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows)


def test_cache_shape_matches_chain_cache(store):
    """Keys must be (kind, offset, strike_offset, option_type) — the shape
    chain_metrics indexes into."""
    t0 = datetime(2026, 8, 17, 11, 30)
    _write(store, "NIFTY", t0)
    cache = store.option_bar_cache_asof("NIFTY", t0, max_age_min=10)
    assert cache
    k = next(iter(cache))
    assert len(k) == 4 and k[0] == "WEEKLY" and k[3] in ("CALL", "PUT")


def test_chain_metrics_consumes_it_unchanged(store):
    """The whole point: same pure function, either source."""
    t0 = datetime(2026, 8, 17, 11, 30)
    _write(store, "NIFTY", t0)
    m = scanner.chain_metrics(store.option_bar_cache_asof("NIFTY", t0))
    assert m["atm_iv"] is not None
    assert m["iv_skew"] is not None
    assert m["call_oi"] > 0 and m["put_oi"] > 0


def test_skew_sign_matches_the_live_definition(store):
    """skew = OTM-put IV minus OTM-call IV. Puts richer -> positive."""
    t0 = datetime(2026, 8, 17, 11, 30)
    _write(store, "NIFTY", t0)          # puts seeded richer than calls
    assert scanner.chain_metrics(store.option_bar_cache_asof("NIFTY", t0))["iv_skew"] > 0


def test_stale_read_rejected(store):
    t0 = datetime(2026, 8, 17, 9, 30)
    _write(store, "NIFTY", t0)
    assert store.option_bar_cache_asof(
        "NIFTY", t0 + timedelta(minutes=45), max_age_min=10) is None


def test_none_when_nothing_recorded(store):
    assert store.option_bar_cache_asof(
        "NIFTY", datetime(2026, 8, 17, 11, 30)) is None


def test_cache_at_reads_option_bars_when_asked(store):
    """_cache_at(source='option_bars') must route to the new reader and still
    pin one expiry."""
    t0 = datetime(2026, 8, 17, 11, 30)
    _write(store, "BANKNIFTY", t0, kind="MONTHLY", off=0, base=57000.0)
    _write(store, "BANKNIFTY", t0, kind="MONTHLY", off=1, base=57000.0)

    cache = _cache_at(store, "BANKNIFTY", t0 + timedelta(minutes=1), 10,
                      source="option_bars")

    assert cache is not None
    assert {k[:2] for k in cache} == {("MONTHLY", 0)}


def test_chain_source_still_reads_chain_snapshots(store):
    """Default source must be unchanged — option_bars data alone must not
    make the chain reader start returning rows."""
    t0 = datetime(2026, 8, 17, 11, 30)
    _write(store, "NIFTY", t0)
    assert _cache_at(store, "NIFTY", t0, 10, source="chain") is None


def test_option_bars_range_reports_real_dates(store):
    """/data/coverage reported a bare count, which cannot tell a table that
    stopped writing from one that never had data."""
    t0 = datetime(2026, 8, 17, 11, 30)
    _write(store, "NIFTY", t0)
    _write(store, "NIFTY", t0 + timedelta(days=3))
    rows = {r[0]: r for r in store.option_bars_range()}
    assert "NIFTY" in rows
    assert rows["NIFTY"][3] == 2          # two distinct sessions
