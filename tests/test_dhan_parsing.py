"""Offline replay tests for the Dhan data adapter.

These exercise the parse -> upsert -> store boundary by replaying REAL sample
responses captured from the live dhanhq SDK and trimmed to 3 rows
(tests/fixtures/*.json). No network, no credentials, no dhanhq install
required — dhan_client imports the SDK lazily, only inside the fetch functions.

Run: venv/Scripts/python -m pytest tests/test_dhan_parsing.py -q
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import duckdb

from app.data import dhan_client as dc
from app.data.store import SCHEMA

FIX = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    d = json.loads((FIX / name).read_text())
    d.pop("_comment", None)
    return d


class _MemStore:
    """Minimal stand-in for DataStore backed by an in-memory DuckDB."""
    def __init__(self):
        self.con = duckdb.connect(":memory:")
        self.con.execute(SCHEMA)


# --- epoch -> IST boundary (invariant #7) ----------------------------------

def test_epoch_to_ist():
    # 1780285800 == 2026-06-01 03:50:00 UTC == 09:20:00 IST (first intraday bar)
    assert dc._epoch_to_ist(1780285800) == datetime(2026, 6, 1, 9, 20, 0)
    # accepts float epochs too (intraday timestamps come back as floats)
    assert dc._epoch_to_ist(1780285800.0) == datetime(2026, 6, 1, 9, 20, 0)


# --- intraday underlying candles (single-nested: data = {open,...}) ---------

def test_parse_and_upsert_intraday():
    data = _load("intraday_nifty.json")
    rows = dc.parse_intraday_rows("NIFTY", data)
    assert len(rows) == 3
    # (underlying, ts, open, high, low, close, volume, oi)
    assert rows[0][0] == "NIFTY"
    assert rows[0][1] == datetime(2026, 6, 1, 9, 20, 0)  # IST, not UTC
    assert rows[0][3] == 23646.05  # high
    assert rows[0][5] == 23598.9   # close
    assert rows[0][7] == 0         # oi defaulted (oi=False on this call)

    store = _MemStore()
    assert dc.upsert_underlying_rows(store, rows) == 3
    # idempotent: re-upsert replaces, doesn't duplicate (PK underlying, ts)
    dc.upsert_underlying_rows(store, rows)
    n = store.con.execute("SELECT count(*) FROM underlying_bars").fetchone()[0]
    assert n == 3
    lo, hi = store.con.execute(
        "SELECT min(ts), max(ts) FROM underlying_bars WHERE underlying='NIFTY'").fetchone()
    assert lo == datetime(2026, 6, 1, 9, 20, 0)
    assert hi == datetime(2026, 6, 1, 9, 30, 0)


# --- expired options (double-nested: data = {data: {ce, pe}}) ---------------

def test_parse_expired_option_call_side():
    data = _load("expired_option_nifty_atm_call.json")
    rows = dc.parse_expired_option_rows("NIFTY", 0, "CALL", data)
    assert len(rows) == 3
    # (underlying, ts, expiry_kind, expiry_offset, strike_offset, option_type,
    #  strike, expiry, o, h, l, c, volume, oi, iv)
    r = rows[0]
    assert r[1] == datetime(2026, 6, 8, 9, 15, 0)  # epoch -> IST through the wrapper
    assert r[2] == "WEEKLY" and r[3] == 0 and r[4] == 0
    assert r[5] == "CALL"       # option_type stored as OptionType.value
    assert r[6] == 23150.0      # absolute rolling-ATM strike from ce.strike[]
    assert r[7] is None         # expiry not provided by rolling API -> NULL
    assert r[11] == 125.55      # close from data.data.ce.close[]
    assert r[14] == 21.924944   # iv from data.data.ce.iv[]


def test_call_request_has_no_put_side():
    # When CALL is requested the live API returns data.pe = null; asking the
    # parser for the PUT side must yield nothing, not crash.
    data = _load("expired_option_nifty_atm_call.json")
    assert dc.parse_expired_option_rows("NIFTY", 0, "PUT", data) == []


def test_parse_expired_option_put_side_reads_pe():
    data = _load("expired_option_nifty_atm_put.json")
    rows = dc.parse_expired_option_rows("NIFTY", 0, "PUT", data)
    assert len(rows) == 3
    assert rows[0][5] == "PUT"
    assert rows[0][11] == 121.2  # data.data.pe.close[0], distinct from the CALL


def test_upsert_options_and_coverage_query():
    """End-to-end: parse both real sides, upsert, and confirm the
    /data/coverage aggregation and an option_close-style lookup."""
    call = _load("expired_option_nifty_atm_call.json")
    put = _load("expired_option_nifty_atm_put.json")
    store = _MemStore()
    dc.upsert_option_rows(store, dc.parse_expired_option_rows("NIFTY", 0, "CALL", call))
    dc.upsert_option_rows(store, dc.parse_expired_option_rows("NIFTY", 0, "PUT", put))

    # CALL and PUT differ only in option_type, so both survive the PK.
    total = store.con.execute("SELECT count(*) FROM option_bars").fetchone()[0]
    assert total == 6

    # Mirror of the /data/coverage aggregation.
    opt = dict(store.con.execute(
        "SELECT underlying, count(*) FROM option_bars GROUP BY underlying").fetchall())
    assert opt["NIFTY"] == 6

    # option_close-style lookup resolves the ATM CALL close at/after a ts.
    close = store.con.execute(
        """SELECT close FROM option_bars
           WHERE underlying='NIFTY' AND expiry_kind='WEEKLY' AND expiry_offset=0
             AND strike_offset=0 AND option_type='CALL' AND ts <= ?
           ORDER BY ts DESC LIMIT 1""", [datetime(2026, 6, 8, 9, 25)]).fetchone()
    assert close[0] == 137.0  # 3rd (last) CALL close in the trimmed fixture
