"""Event-window signal report — expiry selection.

Offline. The report used to filter its chain cache on a hardcoded
("WEEKLY", 0). NSE discontinued BANKNIFTY's weeklies and MCX options are
monthly-only, so chain.effective_targets records those names under MONTHLY —
and the filter therefore dropped 100% of their rows and printed "no
chain_snapshots recorded near this window" while the data sat in the table.
Observed 2026-08-17 on a real BANKNIFTY window.

These pin the fix: the front expiry is derived FROM THE DATA, so a
monthly-only name reads its real chain, a weekly name is unchanged, and the
report names the contract it used.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.data.store import DataStore
from app.engines.event_signals import _cache_at, _front_expiry, build_report


@pytest.fixture
def store(tmp_path):
    return DataStore(tmp_path / "md.duckdb")


def _chain_row(u, ts, soff, otype, oi, strike, kind="WEEKLY", off=0, spot=22000.0):
    # matches chain_snapshots column order
    return (u, ts, None, kind, off, strike, soff, otype, spot,
            100.0, 99.5, 100.5, 14.0, oi, 500, None, None, None, None)


def _four_strikes(u, ts, kind, off, base=22000.0):
    return [
        _chain_row(u, ts, 0, "CALL", 1000, base, kind, off, base),
        _chain_row(u, ts, 0, "PUT", 1500, base, kind, off, base),
        _chain_row(u, ts, -1, "PUT", 800, base - 50, kind, off, base),
        _chain_row(u, ts, 1, "CALL", 700, base + 50, kind, off, base),
    ]


# --- _front_expiry ----------------------------------------------------------

def test_front_expiry_picks_monthly_when_that_is_all_there_is():
    raw = {("MONTHLY", 0, 0, "CALL"): object(), ("MONTHLY", 0, 0, "PUT"): object()}
    assert _front_expiry(raw) == ("MONTHLY", 0)


def test_front_expiry_prefers_lowest_offset():
    raw = {("MONTHLY", 1, 0, "CALL"): object(), ("MONTHLY", 0, 0, "CALL"): object()}
    assert _front_expiry(raw) == ("MONTHLY", 0)


def test_front_expiry_prefers_weekly_on_an_offset_tie():
    """A name recording both cycles: WEEKLY 0 is the nearer contract."""
    raw = {("MONTHLY", 0, 0, "CALL"): object(), ("WEEKLY", 0, 0, "CALL"): object()}
    assert _front_expiry(raw) == ("WEEKLY", 0)


def test_front_expiry_none_on_empty():
    assert _front_expiry({}) is None


# --- _cache_at (the regression) ---------------------------------------------

def test_monthly_only_underlying_is_readable(store):
    """THE bug: BANKNIFTY records MONTHLY and returned nothing at all."""
    t0 = datetime(2026, 8, 17, 11, 30)
    store.upsert_chain_rows(_four_strikes("BANKNIFTY", t0, "MONTHLY", 0, 57000.0))

    cache = _cache_at(store, "BANKNIFTY", t0 + timedelta(minutes=1), 10)

    assert cache is not None, "monthly-only chain must not be filtered away"
    assert len(cache) == 4
    assert {k[:2] for k in cache} == {("MONTHLY", 0)}


def test_weekly_underlying_unchanged(store):
    """The fix must not re-point a name that genuinely has weeklies."""
    t0 = datetime(2026, 8, 17, 11, 30)
    rows = _four_strikes("NIFTY", t0, "WEEKLY", 0)
    rows += _four_strikes("NIFTY", t0, "MONTHLY", 0)   # also recorded, further out
    store.upsert_chain_rows(rows)

    cache = _cache_at(store, "NIFTY", t0 + timedelta(minutes=1), 10)

    assert {k[:2] for k in cache} == {("WEEKLY", 0)}


def test_cache_is_homogeneous_so_callers_can_recover_the_expiry(store):
    """build_report reads the group off any key — that only holds if the
    returned cache never mixes expiries."""
    t0 = datetime(2026, 8, 17, 11, 30)
    rows = _four_strikes("NIFTY", t0, "WEEKLY", 0)
    rows += _four_strikes("NIFTY", t0, "WEEKLY", 1)
    store.upsert_chain_rows(rows)

    cache = _cache_at(store, "NIFTY", t0 + timedelta(minutes=1), 10)

    assert len({k[:2] for k in cache}) == 1


def test_still_none_when_nothing_recorded(store):
    """An empty read must stay honestly empty, not become a false positive."""
    assert _cache_at(store, "NIFTY", datetime(2026, 8, 17, 11, 30), 10) is None


def test_stale_snapshot_still_rejected(store):
    t0 = datetime(2026, 8, 17, 9, 30)
    store.upsert_chain_rows(_four_strikes("BANKNIFTY", t0, "MONTHLY", 0, 57000.0))
    assert _cache_at(store, "BANKNIFTY", t0 + timedelta(minutes=45), 10) is None


# --- report ------------------------------------------------------------------

def test_report_reads_monthly_and_names_the_expiry(store):
    t0 = datetime(2026, 8, 17, 11, 30)
    for off_min in (-10, -5, 0):
        store.upsert_chain_rows(
            _four_strikes("BANKNIFTY", t0 + timedelta(minutes=off_min),
                          "MONTHLY", 0, 57000.0))

    out = build_report(store, "BANKNIFTY", t0, before=10, after=0, sample=5)

    assert "no chain_snapshots recorded near this window" not in out
    assert "Expiry read: MONTHLY+0" in out
    assert "MIXED" not in out


# --- zero-OI max pain / pinned expiry (2026-08-17) --------------------------

def test_max_pain_none_on_zero_oi_chain():
    """A next-month chain with strikes but no OI ties every strike at pain 0,
    and the loop would return the LOWEST strike as if it were a level. The
    BANKNIFTY window printed 'max pain 31000' against a 57180 spot."""
    from app.core.contract import OptionType
    from app.data.store import OptionQuote
    from app.engines import scanner
    ts = datetime(2026, 8, 17, 11, 30)
    cache = {}
    for i, strike in enumerate((31000.0, 57000.0, 59000.0)):
        for otype in ("CALL", "PUT"):
            cache[("MONTHLY", 1, i, otype)] = OptionQuote(
                ts, "BANKNIFTY", None, strike, OptionType(otype), 100.0, oi=0)
    assert scanner.max_pain(cache) is None


def test_max_pain_still_works_with_real_oi():
    from app.core.contract import OptionType
    from app.data.store import OptionQuote
    from app.engines import scanner
    ts = datetime(2026, 8, 17, 11, 30)
    cache = {
        ("MONTHLY", 0, 0, "CALL"): OptionQuote(ts, "X", None, 100.0, OptionType("CALL"), 5.0, oi=5000),
        ("MONTHLY", 0, 0, "PUT"): OptionQuote(ts, "X", None, 100.0, OptionType("PUT"), 5.0, oi=5000),
        ("MONTHLY", 0, 1, "CALL"): OptionQuote(ts, "X", None, 110.0, OptionType("CALL"), 1.0, oi=10),
    }
    assert scanner.max_pain(cache) == 100.0


def test_window_pins_one_expiry_and_drops_off_expiry_samples(store):
    """A sample whose freshest batch is the NEXT expiry must not be read as if
    it were the front one — _top_oi_moves would difference two contracts."""
    t0 = datetime(2026, 8, 17, 11, 30)
    # -10m and event: front month. -5m: only next month recorded.
    store.upsert_chain_rows(
        _four_strikes("BANKNIFTY", t0 - timedelta(minutes=10), "MONTHLY", 0, 57000.0))
    store.upsert_chain_rows(
        _four_strikes("BANKNIFTY", t0 - timedelta(minutes=5), "MONTHLY", 1, 57000.0))
    store.upsert_chain_rows(
        _four_strikes("BANKNIFTY", t0, "MONTHLY", 0, 57000.0))

    out = build_report(store, "BANKNIFTY", t0, before=10, after=0, sample=5,
                       max_age_min=3)

    assert "Expiry read: MONTHLY+0" in out
    assert "1 sample(s) dropped" in out
    assert "MIXED" not in out
