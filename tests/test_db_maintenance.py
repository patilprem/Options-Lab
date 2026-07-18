"""Automated DB maintenance: purge only off-hours/weekend JUNK, never real
session data, then CHECKPOINT. The nightly _nightly_maintenance task relies on
these two DataStore primitives — this locks in that real recorded rows survive
(the recording is the research product; maintenance must not eat it).
"""

from __future__ import annotations

from datetime import datetime

from app.data.store import DataStore

# 2026-07-16 is a Thursday (weekday); 2026-07-18 is a Saturday (weekend).
NSE_IN = datetime(2026, 7, 16, 11, 0)     # NSE session      -> KEEP
NSE_PRE = datetime(2026, 7, 16, 8, 0)     # before open      -> junk
WEEKEND = datetime(2026, 7, 18, 11, 0)    # Saturday         -> junk
MCX_IN = datetime(2026, 7, 16, 22, 0)     # MCX session      -> KEEP
MCX_LATE = datetime(2026, 7, 16, 23, 55)  # after MCX close  -> junk


def _seed(st):
    c = st.con
    for u, ts in [("NIFTY", NSE_IN), ("NIFTY", NSE_PRE), ("NIFTY", WEEKEND)]:
        c.execute("INSERT INTO underlying_bars VALUES (?,?,?,?,?,?,?,?)",
                  [u, ts, 100, 100, 100, 100, 0, 0])
    rows = [("NIFTY", NSE_IN), ("NIFTY", NSE_PRE), ("NIFTY", WEEKEND),
            ("CRUDEOIL", MCX_IN), ("CRUDEOIL", MCX_LATE), ("CRUDEOIL", WEEKEND)]
    for u, ts in rows:
        c.execute(
            "INSERT INTO option_bars (underlying, ts, expiry_kind, expiry_offset,"
            " strike_offset, option_type, strike, expiry, open, high, low, close,"
            " volume, oi, iv) VALUES (?,?,'WEEKLY',0,0,'CALL',100,?,1,1,1,1,0,0,0)",
            [u, ts, ts.date()])
        c.execute(
            "INSERT INTO chain_snapshots (underlying, ts, expiry, expiry_kind,"
            " expiry_offset, strike, strike_offset, option_type, spot, ltp, bid,"
            " ask, iv, oi, volume, delta, theta, vega, gamma)"
            " VALUES (?,?,?,'WEEKLY',0,100,0,'CALL',0,1,1,1,0,0,0,0,0,0,0)",
            [u, ts, ts.date()])


def _count(st, table, u, ts):
    return st.con.execute(
        f"SELECT count(*) FROM {table} WHERE underlying=? AND ts=?",
        [u, ts]).fetchone()[0]


def test_purge_keeps_session_data_drops_junk(tmp_path):
    st = DataStore(tmp_path / "m.duckdb")
    _seed(st)

    res = st.purge_offhours()

    # real session rows survive in every table
    assert _count(st, "underlying_bars", "NIFTY", NSE_IN) == 1
    for table in ("option_bars", "chain_snapshots"):
        assert _count(st, table, "NIFTY", NSE_IN) == 1, table
        assert _count(st, table, "CRUDEOIL", MCX_IN) == 1, table   # MCX late session kept
        # junk gone: pre-open, post-MCX-close, and weekend
        assert _count(st, table, "NIFTY", NSE_PRE) == 0, table
        assert _count(st, table, "CRUDEOIL", MCX_LATE) == 0, table
        assert _count(st, table, "CRUDEOIL", WEEKEND) == 0, table
    assert _count(st, "underlying_bars", "NIFTY", WEEKEND) == 0
    assert res["chain_snapshots"]["deleted"] >= 3


def test_checkpoint_runs(tmp_path):
    st = DataStore(tmp_path / "m.duckdb")
    _seed(st)
    st.purge_offhours()
    st.checkpoint()          # must not raise; reclaims the freed pages
