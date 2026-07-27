"""
/data/health — per-table recorder freshness audit.

Exists because the 2026-07-23..27 outage was INVISIBLE: chain_snapshots
stopped for NIFTY/BANKNIFTY/CRUDEOIL/GOLD for five days while /data/coverage
still looked healthy. Coverage reports underlying_bars dates plus a bare
option_bars COUNT, so "stopped writing" and "never had data" render
identically. These tests pin the two properties that make the audit
trustworthy: a table that stops writing during an open session is flagged,
and a quiet table OUTSIDE session hours is NOT (a flag that cries wolf
off-hours trains you to ignore it).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import app.api.strategies as api
from app.data.store import SyntheticStore


class _Store:
    """Minimal stand-in exposing just recording_health()."""
    def __init__(self, rows):
        self._rows = rows

    def recording_health(self):
        return [dict(r) for r in self._rows]


def _fresh(minutes_ago: float) -> str:
    from app.engines.paper import IST
    now = datetime.now(IST).replace(tzinfo=None)
    return (now - timedelta(minutes=minutes_ago)).isoformat()


def _row(table, last_ts, segments=("NSE", "MCX"), periodic=True,
         present=True, rows_today=0, sessions=1, error=None):
    return {"table": table, "present": present, "last_ts": last_ts,
            "rows_today": rows_today, "sessions": sessions, "error": error,
            "segments": list(segments), "periodic": periodic}


def _health(monkeypatch, rows, nse=True, mcx=False, stale_min=20):
    monkeypatch.setattr(api, "_store", _Store(rows))
    monkeypatch.setattr("app.engines.watchdog.session_open_for",
                        lambda segs, now, **kw: nse if "NSE" in segs else mcx)
    return api.data_health(stale_min=stale_min)


def test_synthetic_store_reports_nothing_recorded(monkeypatch):
    monkeypatch.setattr(api, "_store", SyntheticStore())
    out = api.data_health()
    assert out["synthetic"] is True and out["tables"] == []


def test_stalled_table_is_flagged_during_an_open_session(monkeypatch):
    """The exact 07-23..27 signature: one table silently stops while the
    others keep writing."""
    out = _health(monkeypatch, [
        _row("option_bars", _fresh(2), rows_today=900),
        _row("chain_snapshots", _fresh(400)),
    ])
    by = {t["table"]: t for t in out["tables"]}
    assert by["option_bars"]["stale"] is False
    assert by["chain_snapshots"]["stale"] is True
    assert out["any_stale"] is True


def test_quiet_tables_are_not_stale_outside_session_hours(monkeypatch):
    """Off-hours silence is CORRECT — flagging it would make the flag
    worthless."""
    out = _health(monkeypatch, [
        _row("chain_snapshots", _fresh(600)),
    ], nse=False, mcx=False)
    assert out["tables"][0]["stale"] is False
    assert out["any_stale"] is False


def test_never_written_table_is_stale_when_session_open(monkeypatch):
    """last_ts None during an open session means it has never written —
    that must flag, not pass silently."""
    out = _health(monkeypatch, [
        _row("index_bias_history", None, segments=("NSE",)),
    ])
    assert out["tables"][0]["stale"] is True


def test_missing_table_is_reported_not_raised(monkeypatch):
    out = _health(monkeypatch, [
        _row("setup_flags", None, present=False, periodic=False,
             error="CatalogException: nope"),
    ])
    row = out["tables"][0]
    assert row["present"] is False and row["error"]
    assert row["stale"] is False        # absent != stalled


def test_age_minutes_is_computed(monkeypatch):
    out = _health(monkeypatch, [
        _row("underlying_bars", _fresh(7), rows_today=80),
    ])
    assert 6.0 <= out["tables"][0]["age_min"] <= 8.5


def test_nse_only_table_not_flagged_while_only_mcx_is_open(monkeypatch):
    """The false alarm found the first evening this endpoint ran live:
    index_bias_history/stock_snapshots come from the NSE stock sweep and
    correctly stop at 15:35, but MCX trades to 23:30 -- judging them against
    "any session open" flagged them every night for 8 hours."""
    out = _health(monkeypatch, [
        _row("index_bias_history", _fresh(58), segments=("NSE",)),
        _row("stock_snapshots", _fresh(58), segments=("NSE",)),
        _row("chain_snapshots", _fresh(2), segments=("NSE", "MCX")),
    ], nse=False, mcx=True)
    by = {t["table"]: t for t in out["tables"]}
    assert by["index_bias_history"]["stale"] is False
    assert by["stock_snapshots"]["stale"] is False
    assert by["chain_snapshots"]["stale"] is False
    assert out["any_stale"] is False


def test_event_driven_table_is_never_flagged(monkeypatch):
    """setup_flags only writes when a setup clears the alert threshold, so a
    quiet stretch is normal and age says nothing about health."""
    out = _health(monkeypatch, [
        _row("setup_flags", _fresh(414), segments=("NSE",), periodic=False),
    ], nse=True)
    assert out["tables"][0]["stale"] is False


def test_mcx_fed_table_still_flagged_in_the_mcx_evening(monkeypatch):
    """The flip side: a table MCX feeds must still be caught after the NSE
    close -- that is exactly when the 07-23..27 chain outage was running."""
    out = _health(monkeypatch, [
        _row("chain_snapshots", _fresh(400), segments=("NSE", "MCX")),
    ], nse=False, mcx=True)
    assert out["tables"][0]["stale"] is True
