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


_HDR = ("SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,"
        "SEM_EXPIRY_CODE,SEM_TRADING_SYMBOL,SEM_LOT_UNITS,SEM_CUSTOM_SYMBOL,"
        "SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,SEM_OPTION_TYPE,SEM_TICK_SIZE,"
        "SEM_EXPIRY_FLAG,SEM_EXCH_INSTRUMENT_TYPE,SEM_SERIES,SM_SYMBOL_NAME")
# Far-future expiries so "non-expired" is true regardless of the test clock.
_FAKE_MASTER = "\n".join([
    _HDR,
    "NSE,E,2885,EQUITY,0,RELIANCE,1,RELIANCE,,0,,0.05,,ES,EQ,RELIANCE",
    "NSE,D,46376,FUTSTK,1,RELIANCE-Jul2099-FUT,500,RELIANCE JUL FUT,2099-07-31,0,,0.05,M,FS,,RELIANCE",
    "NSE,D,35002,FUTIDX,2,NIFTY-Aug2099-FUT,75,NIFTY AUG FUT,2099-08-28,0,,0.05,M,FI,,NIFTY",
    "NSE,D,35001,FUTIDX,1,NIFTY-Jul2099-FUT,75,NIFTY JUL FUT,2099-07-31,0,,0.05,M,FI,,NIFTY",
    "BSE,D,824001,FUTIDX,1,SENSEX-Jul2099-FUT,20,SENSEX JUL FUT,2099-07-30,0,,0.05,M,FI,,SENSEX",
])


def _patch_master(monkeypatch, tmp_path):
    """Point both caches at tmp and serve _FAKE_MASTER for every download."""
    import urllib.request

    class _Resp:
        def read(self):
            return _FAKE_MASTER.encode("utf-8")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    monkeypatch.setattr(dc, "_FNO_MASTER_CACHE", tmp_path / "fno.csv")
    monkeypatch.setattr(dc, "_IDX_FUT_MASTER_CACHE", tmp_path / "idxfut.csv")


def test_scanner_cache_write_does_not_starve_index_futures(monkeypatch, tmp_path):
    """Regression (2026-07-20 "[1] EMPTY — no FUTIDX rows matched"): the scanner
    rewrites its master cache keeping FUTSTK/EQUITY only. If index-futures
    resolution shared that file it would read zero FUTIDX rows. Separate caches
    must keep both working no matter the order."""
    _patch_master(monkeypatch, tmp_path)

    # Scanner runs first and trims its cache to FUTSTK/EQUITY (drops FUTIDX)...
    uni = dc.resolve_fno_universe(today=date(2026, 7, 16))
    assert set(uni) == {"RELIANCE"}
    assert "FUTIDX" not in (tmp_path / "fno.csv").read_text()

    # ...index-futures resolution must STILL find its FUTIDX rows.
    fut = dc.resolve_index_futures()
    assert fut["NIFTY"]["security_id"] == 35001        # nearest month, not Aug
    assert fut["SENSEX"]["security_id"] == 824001       # BSE index future kept
    assert "FUTIDX" in (tmp_path / "idxfut.csv").read_text()


def test_index_futures_cache_holds_only_futidx(monkeypatch, tmp_path):
    """The dedicated cache is trimmed to FUTIDX rows — no equity/stock-future
    bloat leaks in, and the scanner's separate cache is untouched."""
    _patch_master(monkeypatch, tmp_path)
    dc.resolve_index_futures()
    body = (tmp_path / "idxfut.csv").read_text()
    assert "FUTIDX" in body and "FUTSTK" not in body and "EQUITY" not in body
    assert not (tmp_path / "fno.csv").exists()   # index resolve never wrote it


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
