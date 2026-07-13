"""M-data: backfill period math + request wiring + resume-skip boundary (offline)."""

from datetime import date, datetime

import duckdb
import pytest

from app.api.strategies import BackfillReq, _period_dates, _chunks, _PERIODS
from app.core import registry
from app.data import dhan_client as dc
from app.data.store import SCHEMA


def test_chunks_ceil():
    assert _chunks(90, 89) == 2
    assert _chunks(89, 89) == 1
    assert _chunks(1, 29) == 1
    assert _chunks(365, 29) == 13


def test_period_dates_max_is_five_years():
    start, end = _period_dates(BackfillReq(period="max"))
    assert end == date.today()
    assert 1820 <= (end - start).days <= 1826     # ~5 years


def test_period_dates_named_windows():
    for period, days in (("3m", 90), ("6m", 182), ("1y", 365), ("2y", 730)):
        start, end = _period_dates(BackfillReq(period=period))
        assert (end - start).days == days


def test_period_dates_explicit_override():
    start, end = _period_dates(BackfillReq(from_date="2024-01-01", to_date="2024-06-30"))
    assert start == date(2024, 1, 1) and end == date(2024, 6, 30)


def test_all_periods_present():
    assert set(_PERIODS) >= {"3m", "6m", "1y", "2y", "5y", "max"}


def _memstore():
    s = type("S", (), {"con": duckdb.connect(":memory:")})()
    s.con.execute(SCHEMA)
    return s


def test_resume_uses_chunk_ledger_not_row_existence(tmp_path, monkeypatch):
    """Rows-in-range must NOT imply 'done' — a partially written chunk would be
    skipped forever, silently gapping the data. Only the ledger marks done."""
    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "r.db")
    registry.init_db()
    store = _memstore()
    # stray rows inside chunk 2's range (as a killed/concurrent run would leave)
    store.con.execute("INSERT INTO underlying_bars VALUES ('NIFTY', ?, 1,1,1,1,0,0)",
                      [datetime(2024, 11, 1, 9, 15)])

    fetched = []
    monkeypatch.setattr(dc, "fetch_intraday", lambda *a, **k: fetched.append(a[5][:10]) or {})
    monkeypatch.setattr(dc, "fetch_expired_option", lambda *a, **k: {})

    # two 89-day chunks: [07-10→10-07], [10-07→01-04]
    dc.backfill("NIFTY", date(2024, 7, 10), date(2025, 1, 4),
                strike_offsets=range(0, 0), interval=5, client=object(), store=store)

    # despite stray rows, BOTH chunks are fetched (ledger was empty)
    assert fetched == ["2024-07-10", "2024-10-07"]
    assert registry.count_chunks("NIFTY") == 2

    # re-run resumes: both now recorded done -> nothing re-fetched
    fetched.clear()
    dc.backfill("NIFTY", date(2024, 7, 10), date(2025, 1, 4),
                strike_offsets=range(0, 0), interval=5, client=object(), store=store)
    assert fetched == []


def test_fetch_retries_transient_errors(tmp_path, monkeypatch):
    """A transient network blip on one chunk must NOT abort the whole backfill:
    the fetch retries and succeeds."""
    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "r.db")
    registry.init_db()
    monkeypatch.setattr(dc.time, "sleep", lambda *_: None)   # no real backoff wait
    store = _memstore()

    calls = {"n": 0}
    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:                       # fail twice, then succeed
            raise ConnectionResetError("forcibly closed")
        return {}
    monkeypatch.setattr(dc, "fetch_intraday", flaky)
    monkeypatch.setattr(dc, "fetch_expired_option", lambda *a, **k: {})

    dc.backfill("NIFTY", date(2024, 7, 10), date(2024, 9, 1),
                strike_offsets=range(0, 0), interval=5, client=object(), store=store)
    assert calls["n"] == 3                        # 2 failures + 1 success
    assert registry.count_chunks("NIFTY") == 1    # chunk completed after retries


def test_chunk_ledger_marks_only_after_success(tmp_path, monkeypatch):
    """If a fetch raises, the chunk must NOT be marked done (so it retries)."""
    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "r.db")
    registry.init_db()
    store = _memstore()

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(dc, "fetch_intraday", boom)
    monkeypatch.setattr(dc, "fetch_expired_option", lambda *a, **k: {})
    with pytest.raises(RuntimeError):
        dc.backfill("NIFTY", date(2024, 7, 10), date(2024, 9, 1),
                    strike_offsets=range(0, 0), interval=5, client=object(), store=store)
    assert registry.count_chunks("NIFTY") == 0   # nothing marked done
