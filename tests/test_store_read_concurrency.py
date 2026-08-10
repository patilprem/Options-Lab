"""Reads must not queue behind writes.

2026-08-11, measured on the VPS: /data/health sat flat at 0.117s and then
stalled for 18-30 SECONDS once every 5m56s — exactly the chain recorder's
cadence. One `threading.Lock` guarded both reads and writes, so every reader
waited out the recorder's ~4,500-row INSERT OR REPLACE into a multi-million-row
table. scripts/diag.py (10s HTTP timeout) reported RED against a recorder that
was perfectly healthy.

The dashboard freezing was the visible cost. The one that mattered: hub.quote()
and quote_position() fall back to the store on a chain-cache miss, so a
stop-loss check could queue behind the recorder for half a minute.

Reads now use a per-thread cursor — a separate DuckDB connection over the same
database, reading an MVCC snapshot — and never take the write lock. These tests
hold the lock explicitly rather than timing a real write, so they pin the
PROPERTY (a reader does not wait for `_lock`) instead of racing a wall clock.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

import pytest

from app.data import dhan_client as dc
from app.data.store import DataStore

# Long enough that a reader which DOES take the lock is unmistakable, short
# enough that a regression fails instead of hanging the suite.
HOLD_S = 3.0
FAST_S = 1.0


@pytest.fixture
def store(tmp_path):
    s = DataStore(tmp_path / "m.duckdb")
    s.upsert_live_bar("NIFTY", _bar(datetime(2026, 8, 11, 10, 0)))
    return s


class _Bar:
    def __init__(self, ts):
        self.ts, self.open, self.high, self.low, self.close = ts, 1.0, 2.0, 0.5, 1.5
        self.volume, self.oi = 10.0, 5.0


def _bar(ts):
    return _Bar(ts)


class _HeldLock:
    """Holds the store's WRITE lock in a background thread, the way an
    in-flight recorder upsert does."""

    def __init__(self, store, hold_s: float = HOLD_S):
        self.store, self.hold_s = store, hold_s
        self.held = threading.Event()
        self.release = threading.Event()

    def __enter__(self):
        self.thread = threading.Thread(target=self._hold, daemon=True)
        self.thread.start()
        assert self.held.wait(5), "writer never acquired the lock"
        return self

    def _hold(self):
        with self.store._lock:
            self.held.set()
            # Times out on its own so a reader that DOES block fails the
            # assertion instead of deadlocking the test run.
            self.release.wait(self.hold_s)

    def __exit__(self, *exc):
        self.release.set()
        self.thread.join(5)
        return False


def _timed(fn):
    t = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - t


# --- the property ----------------------------------------------------------

def test_a_read_completes_while_a_writer_holds_the_lock(store):
    """THE regression guard. Put `with self._lock` back into _q/_q1 and this
    read waits out the writer."""
    with _HeldLock(store):
        row, elapsed = _timed(
            lambda: store._q1("SELECT count(*) FROM underlying_bars"))
    assert row is not None and row[0] == 1
    assert elapsed < FAST_S, (
        f"read took {elapsed:.2f}s while a writer held the lock — reads are "
        f"queueing behind writes again")


def test_many_threads_read_at_once_during_a_write(store):
    """Each thread builds its own cursor on first use, including mid-write."""
    results, errors = [], []

    def read():
        try:
            results.append(store._q1("SELECT count(*) FROM underlying_bars")[0])
        except Exception as e:                      # pragma: no cover
            errors.append(repr(e))

    with _HeldLock(store):
        threads = [threading.Thread(target=read) for _ in range(8)]
        t = time.perf_counter()
        for th in threads:
            th.start()
        for th in threads:
            th.join(FAST_S * 2)
        elapsed = time.perf_counter() - t
    assert errors == [], errors
    assert results == [1] * 8
    assert elapsed < FAST_S * 2


def test_a_read_sees_a_write_that_has_already_returned(store):
    """MVCC must not mean STALE. Statements auto-commit, so only an in-flight
    write is invisible to a reader — a returned one is not."""
    before = store._q1("SELECT count(*) FROM underlying_bars")[0]
    store.upsert_live_bar("NIFTY", _bar(datetime(2026, 8, 11, 10, 5)))
    assert store._q1("SELECT count(*) FROM underlying_bars")[0] == before + 1


def test_a_read_sees_a_write_made_on_another_thread(store):
    """The reader cursor is per-thread; a write from elsewhere must still be
    visible once it has committed."""
    def write():
        store.upsert_live_bar("NIFTY", _bar(datetime(2026, 8, 11, 10, 10)))

    before = store._q1("SELECT count(*) FROM underlying_bars")[0]
    th = threading.Thread(target=write)
    th.start()
    th.join(5)
    assert store._q1("SELECT count(*) FROM underlying_bars")[0] == before + 1


# --- writers must still serialise ------------------------------------------

def test_a_bulk_upsert_waits_for_the_write_lock(store):
    """dhan_client's row upserts went straight at store.con, so they were
    never synchronised against the writers inside DataStore — two writers on
    one DuckDB connection. They route through bulk_write now, so unlike a
    reader they SHOULD block here."""
    rows = [("NIFTY", datetime(2026, 8, 11, 10, 15), "WEEKLY", 0, 0, "CALL",
             100.0, None, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 12.0)]
    with _HeldLock(store, hold_s=1.0):
        _, elapsed = _timed(lambda: dc.upsert_option_rows(store, rows))
    assert elapsed >= 0.5, (
        f"bulk upsert returned in {elapsed:.2f}s while another writer held the "
        f"lock — it is bypassing bulk_write and writing unsynchronised")
    assert store._q1("SELECT count(*) FROM option_bars")[0] == 1


def test_a_bulk_upsert_still_works_on_a_bare_stand_in():
    """Test stand-ins carry only a connection, no lock."""
    import duckdb
    from app.data.store import SCHEMA
    bare = type("S", (), {})()
    bare.con = duckdb.connect(":memory:")
    bare.con.execute(SCHEMA)
    n = dc.upsert_option_rows(bare, [
        ("NIFTY", datetime(2026, 8, 11, 10, 20), "WEEKLY", 0, 0, "CALL",
         100.0, None, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 12.0)])
    assert n == 1


def test_an_empty_upsert_is_a_no_op(store):
    assert dc.upsert_option_rows(store, []) == 0
