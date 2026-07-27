"""Retroactive index-volume backfill: contract resolution + safe writes.

The dangerous failure here is NOT a crash — it's silently backfilling the
WRONG contract's volume. Index futures roll monthly and Dhan's scrip master
lists only currently-listed contracts, so resolving an old date against
today's master returns the NEXT contract (listed then, but not front month,
and carrying a fraction of the real volume). That produces plausible-looking
wrong data, which is precisely what this project keeps getting burned by.
Most of these tests exist to prove the code refuses instead of guessing.
"""

from datetime import date, datetime

import duckdb
import pytest

from app.data.index_volume import (contract_spans, front_month_for,
                                   volume_rows_from_intraday)


def _master_row(symbol, expiry, sid):
    return {"SEM_INSTRUMENT_NAME": "FUTIDX", "SEM_TRADING_SYMBOL": symbol,
            "SEM_EXPIRY_DATE": expiry, "SEM_SMST_SECURITY_ID": str(sid),
            "SEM_SEGMENT": "D"}


# A master as it looks on 2026-07-27: July's contract still listed (expires
# 07-30), August and September ahead of it.
MASTER = [_master_row("NIFTY-Jul2026-FUT", "2026-07-30", 101),
          _master_row("NIFTY-Aug2026-FUT", "2026-08-27", 102),
          _master_row("NIFTY-Sep2026-FUT", "2026-09-24", 103),
          _master_row("BANKNIFTY-Jul2026-FUT", "2026-07-30", 201),
          _master_row("BANKNIFTY-Aug2026-FUT", "2026-08-27", 202)]


# --- front-month resolution -------------------------------------------------

def test_resolves_the_contract_that_actually_traded_that_day():
    fut = front_month_for(MASTER, "NIFTY", date(2026, 7, 14))
    assert fut["security_id"] == 101          # July, not August

def test_rolls_to_next_contract_after_expiry():
    fut = front_month_for(MASTER, "NIFTY", date(2026, 8, 3))
    assert fut["security_id"] == 102

def test_refuses_when_the_front_month_has_been_delisted():
    """THE important one. Backfilling 2026-06-10 against a master whose June
    contract is gone must NOT silently return July's."""
    fut = front_month_for(MASTER, "NIFTY", date(2026, 6, 10))
    assert fut is None                        # 07-30 is >45d out -> refuse

def test_boundary_of_the_refusal_window():
    # 45 days is the cutoff; just inside resolves, just outside refuses.
    assert front_month_for(MASTER, "NIFTY", date(2026, 6, 16)) is not None
    assert front_month_for(MASTER, "NIFTY", date(2026, 6, 14)) is None

def test_unknown_index_resolves_to_nothing():
    assert front_month_for(MASTER, "SENSEX", date(2026, 7, 14)) is None

def test_nifty_does_not_capture_niftynxt50():
    master = MASTER + [_master_row("NIFTYNXT50-Jul2026-FUT", "2026-07-30", 999)]
    assert front_month_for(master, "NIFTY", date(2026, 7, 14))["security_id"] == 101


# --- span planning ----------------------------------------------------------

def test_consecutive_days_on_one_contract_become_one_span():
    days = [date(2026, 7, d) for d in (14, 15, 16, 17, 20, 21, 22)]
    spans, unresolved = contract_spans(MASTER, "NIFTY", days)
    assert unresolved == []
    assert len(spans) == 1
    assert spans[0]["security_id"] == 101
    assert (spans[0]["first_day"], spans[0]["last_day"]) == (days[0], days[-1])

def test_rollover_splits_into_two_spans():
    days = [date(2026, 7, 29), date(2026, 7, 30),
            date(2026, 8, 3), date(2026, 8, 4)]
    spans, unresolved = contract_spans(MASTER, "NIFTY", days)
    assert unresolved == []
    assert [s["security_id"] for s in spans] == [101, 102]

def test_unresolvable_days_are_reported_not_silently_dropped():
    days = [date(2026, 6, 10), date(2026, 7, 14), date(2026, 7, 15)]
    spans, unresolved = contract_spans(MASTER, "NIFTY", days)
    assert unresolved == [date(2026, 6, 10)]
    assert len(spans) == 1 and spans[0]["security_id"] == 101

def test_a_hole_breaks_the_span_so_gaps_are_not_fetched_over():
    days = [date(2026, 7, 14), date(2026, 6, 10), date(2026, 7, 15)]
    spans, _ = contract_spans(MASTER, "NIFTY", days)
    # sorted -> Jun-10 (unresolved) then the two July days: one clean span.
    assert len(spans) == 1
    assert spans[0]["first_day"] == date(2026, 7, 14)


# --- row building -----------------------------------------------------------

def _epoch(y, m, d, hh, mm):
    """IST wall-clock -> the epoch seconds Dhan would return."""
    from app.data.dhan_client import IST
    return datetime(y, m, d, hh, mm, tzinfo=IST).timestamp()


def test_only_requested_days_are_kept():
    data = {"timestamp": [_epoch(2026, 7, 14, 9, 15), _epoch(2026, 7, 15, 9, 15)],
            "volume": [1000, 2000], "open_interest": [50, 60]}
    rows = volume_rows_from_intraday("NIFTY", data, {date(2026, 7, 14)})
    assert len(rows) == 1
    assert rows[0][1] == datetime(2026, 7, 14, 9, 15)
    assert rows[0][2] == 1000.0 and rows[0][3] == 50.0

def test_zero_volume_bars_are_dropped():
    """Writing 0 over 0 changes nothing and would only inflate the count."""
    data = {"timestamp": [_epoch(2026, 7, 14, 9, 15), _epoch(2026, 7, 14, 9, 20)],
            "volume": [0, 1500], "open_interest": [0, 0]}
    rows = volume_rows_from_intraday("NIFTY", data, {date(2026, 7, 14)})
    assert len(rows) == 1 and rows[0][2] == 1500.0

def test_empty_payload_is_handled():
    assert volume_rows_from_intraday("NIFTY", {}, {date(2026, 7, 14)}) == []


# --- the write path ---------------------------------------------------------

def _store_with(bars):
    from app.data.store import DataStore
    import threading
    s = DataStore.__new__(DataStore)
    s._lock = threading.Lock()
    s.con = duckdb.connect(":memory:")
    s.con.execute("""CREATE TABLE underlying_bars (
        underlying VARCHAR, ts TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE,
        close DOUBLE, volume DOUBLE, oi DOUBLE, PRIMARY KEY (underlying, ts))""")
    for u, ts, close, vol in bars:
        s.con.execute("INSERT INTO underlying_bars VALUES (?,?,?,?,?,?,?,?)",
                      [u, ts, close, close, close, close, vol, 0])
    return s


def test_volume_is_filled_without_touching_price():
    ts = datetime(2026, 7, 14, 9, 15)
    s = _store_with([("NIFTY", ts, 23990.0, 0)])
    assert s.update_bar_volume([("NIFTY", ts, 12345.0, 678.0)]) == 1
    close, vol, oi = s.con.execute(
        "SELECT close, volume, oi FROM underlying_bars").fetchone()
    assert close == 23990.0        # the index's own print, untouched
    assert vol == 12345.0 and oi == 678.0


def test_existing_live_volume_is_never_overwritten():
    """The companion's live figure outranks a retroactive fetch."""
    ts = datetime(2026, 7, 27, 9, 15)
    s = _store_with([("NIFTY", ts, 23990.0, 999.0)])
    assert s.update_bar_volume([("NIFTY", ts, 111.0, 0.0)]) == 0
    assert s.con.execute("SELECT volume FROM underlying_bars").fetchone()[0] == 999.0


def test_never_inserts_bars_that_do_not_exist():
    """A futures series can carry bars we never recorded for the index; those
    must not become new index rows."""
    s = _store_with([("NIFTY", datetime(2026, 7, 14, 9, 15), 23990.0, 0)])
    n = s.update_bar_volume([("NIFTY", datetime(2026, 7, 14, 15, 45), 500.0, 0.0)])
    assert n == 0
    assert s.con.execute("SELECT count(*) FROM underlying_bars").fetchone()[0] == 1


def test_rerunning_is_idempotent():
    ts = datetime(2026, 7, 14, 9, 15)
    s = _store_with([("NIFTY", ts, 23990.0, 0)])
    rows = [("NIFTY", ts, 12345.0, 678.0)]
    assert s.update_bar_volume(rows) == 1
    assert s.update_bar_volume(rows) == 0        # already has volume now


def test_empty_update_is_a_noop():
    assert _store_with([]).update_bar_volume([]) == 0
