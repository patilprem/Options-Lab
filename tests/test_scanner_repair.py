"""
registry.repair_scanner_daily_pnl() — one-time repair for daily_pnl rows
written before ScannerTrader's day-accumulation fix, which left historical
rows holding only the last cycle's incremental realized/fees instead of the
day's true running total. scanner_journal's exit records were never
affected, so they're the source of truth this repairs from.

Offline: isolated SQLite DB per test.
"""

from __future__ import annotations

import pytest

from app.core import registry

SCANNER_ID = "SCANNER"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "test.db")
    registry.init_db()
    return registry


def test_repair_recomputes_realized_and_fees_from_journal(db):
    """A day's corrupted daily_pnl row (left at some small stray value by
    the old per-cycle-overwrite bug) must end up matching the TRUE sum of
    that day's journaled exits, not the corrupted figure."""
    # corrupted: the old bug left only the last cycle's tiny delta
    db.save_paper_day(SCANNER_ID, "2026-07-22", -24.08, 0.0, 55.52, 499_975.92)

    # ground truth: three real exits that day, net -10,048.67 with real fees
    db.record_journal("DRREDDY", "exit", {
        "entry_price": 22.85, "exit_price": 20.75, "entry_fees": 30.02,
        "exit_fees": 48.49, "realized": -1391.01,
    }, ts="2026-07-22T09:35:21")
    db.record_journal("DRREDDY", "exit", {
        "entry_price": 21.05, "exit_price": 23.90, "entry_fees": 29.51,
        "exit_fees": 52.27, "realized": 1699.47,
    }, ts="2026-07-22T10:24:53")
    db.record_journal("LODHA", "exit", {
        "entry_price": 26.50, "exit_price": 26.60, "entry_fees": 31.06,
        "exit_fees": 55.52, "realized": -24.08,
    }, ts="2026-07-22T10:31:41")

    n = db.repair_scanner_daily_pnl(capital=500_000.0)

    assert n == 1
    rows = {r["trade_date"]: r for r in db.performance_rows(SCANNER_ID, "PAPER")}
    row = rows["2026-07-22"]
    assert row["realized"] == pytest.approx(-1391.01 + 1699.47 - 24.08)
    assert row["fees"] == pytest.approx(30.02 + 48.49 + 29.51 + 52.27 + 31.06 + 55.52)
    # equity re-chained from capital, not left at the old corrupted value
    assert row["equity_eod"] == pytest.approx(500_000.0 + row["realized"])


def test_repair_preserves_stored_unrealized(db):
    """unrealized isn't tracked by the journal (it's a live-only mark) — a
    day's stored unrealized must survive the repair untouched."""
    db.save_paper_day(SCANNER_ID, "2026-07-23", -5.0, 6656.25, 10.0, 0.0)
    db.record_journal("TVSMOTOR", "exit", {
        "entry_price": 20.0, "exit_price": 22.0, "entry_fees": 5.0,
        "exit_fees": 5.0, "realized": 925.0,
    }, ts="2026-07-23T11:00:00")

    db.repair_scanner_daily_pnl(capital=500_000.0)

    row = next(r for r in db.performance_rows(SCANNER_ID, "PAPER")
              if r["trade_date"] == "2026-07-23")
    assert row["unrealized"] == 6656.25
    assert row["realized"] == 925.0


def test_repair_is_idempotent(db):
    """Re-running the repair must land on the same numbers, not double-count
    (it always recomputes from the full journal, never adds onto a
    previous repair's output)."""
    db.record_journal("CIPLA", "exit", {
        "entry_price": 28.35, "exit_price": 26.85, "entry_fees": 29.02,
        "exit_fees": 46.31, "realized": -475.41,
    }, ts="2026-07-22T13:15:00")

    db.repair_scanner_daily_pnl(capital=500_000.0)
    first = dict(db.performance_rows(SCANNER_ID, "PAPER")[0])
    db.repair_scanner_daily_pnl(capital=500_000.0)
    second = dict(db.performance_rows(SCANNER_ID, "PAPER")[0])

    assert first == second


def test_repair_handles_multiple_days_and_chains_equity_across_them(db):
    db.record_journal("A", "exit", {"realized": 1000.0}, ts="2026-07-21T10:00:00")
    db.record_journal("B", "exit", {"realized": -400.0}, ts="2026-07-22T10:00:00")

    n = db.repair_scanner_daily_pnl(capital=500_000.0)

    assert n == 2
    rows = {r["trade_date"]: r for r in db.performance_rows(SCANNER_ID, "PAPER")}
    assert rows["2026-07-21"]["equity_eod"] == pytest.approx(501_000.0)
    assert rows["2026-07-22"]["equity_eod"] == pytest.approx(500_600.0)
    assert db.cum_pnl(SCANNER_ID) == pytest.approx(600.0)


def test_recompute_daily_pnl_endpoint(db):
    from app.api.strategies import scanner_recompute_daily_pnl

    db.record_journal("DRREDDY", "exit", {"realized": -1391.01}, ts="2026-07-22T09:35:21")

    result = scanner_recompute_daily_pnl()

    assert result["days_repaired"] == 1
    assert db.cum_pnl(SCANNER_ID) == pytest.approx(-1391.01)
