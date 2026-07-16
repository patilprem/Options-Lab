"""Offline tests for the FNO stock universe resolver (scanner F1).

Replays a trimmed scrip-master fixture (tests/fixtures/scrip_master_fno_sample.csv)
so parse_fno_universe + the store round-trip are verified without network or
credentials. Run: venv/Scripts/python -m pytest tests/test_fno_universe.py -q
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import duckdb

from app.data import dhan_client as dc
from app.data.store import SCHEMA

FIX = Path(__file__).parent / "fixtures"
TODAY = date(2026, 7, 16)   # fixed so expiry filtering is deterministic


def _rows():
    with open(FIX / "scrip_master_fno_sample.csv", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_parses_fno_stocks_only():
    uni = dc.parse_fno_universe(_rows(), today=TODAY)
    # RELIANCE + INFY have stock futures; NIFTY (OPTIDX) and CRUDEOIL (MCX)
    # must NOT appear — they are not NSE FUTSTK.
    assert set(uni) == {"RELIANCE", "INFY"}


def test_near_future_and_spot_pairing():
    uni = dc.parse_fno_universe(_rows(), today=TODAY)
    r = uni["RELIANCE"]
    assert r["future_security_id"] == 46376        # Jul (nearest LIVE), not Jun
    assert r["spot_security_id"] == 2885           # paired cash-equity id
    assert r["lot_size"] == 500
    assert r["fno_segment"] == "NSE_FNO"
    assert r["near_expiry"] == "2026-07-31"


def test_expired_contracts_dropped_but_future_ones_kept():
    uni = dc.parse_fno_universe(_rows(), today=TODAY)
    # Jun-2026 expired -> gone; Jul + Aug remain, ascending.
    assert uni["RELIANCE"]["expiries"] == ["2026-07-31", "2026-08-28"]


def test_symbol_without_future_is_absent():
    # An equity with no live FUTSTK row should not produce a universe entry.
    rows = [r for r in _rows() if not (r["SM_SYMBOL_NAME"] == "INFY"
                                       and r["SEM_INSTRUMENT_NAME"] == "FUTSTK")]
    uni = dc.parse_fno_universe(rows, today=TODAY)
    assert "INFY" not in uni


def test_store_roundtrip():
    import threading

    from app.data.store import DataStore
    con = duckdb.connect(":memory:")
    con.execute(SCHEMA)
    # Reuse the real DataStore methods against an in-memory connection.
    store = DataStore.__new__(DataStore)
    store.con = con
    store._lock = threading.Lock()

    uni = dc.parse_fno_universe(_rows(), today=TODAY)
    n = store.upsert_fno_universe(TODAY, uni)
    assert n == 2

    got = store.fno_universe()          # latest snapshot
    by_sym = {r["symbol"]: r for r in got}
    assert set(by_sym) == {"RELIANCE", "INFY"}
    assert by_sym["RELIANCE"]["future_security_id"] == 46376
    assert by_sym["RELIANCE"]["expiries"] == ["2026-07-31", "2026-08-28"]
    assert by_sym["INFY"]["lot_size"] == 400
