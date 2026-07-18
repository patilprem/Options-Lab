"""Day-roll of PAPER strategy P&L: today's P&L must be persisted into the
performance rows at end of day, and reset to zero on the first bar of the next
session (so the dashboard's "today" never carries yesterday's number). Prior
sessions stay intact and the equity curve chains across the boundary.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.core import registry
from app.core.contract import Action, Bar, ExpiryKind, LegSpec, OptionType
from app.data.store import SyntheticStore
from app.engines.paper import MarketHub, PaperContext


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "test.db")
    registry.init_db()
    return tmp_path


def _running_record():
    rec = registry.create("PBK", "x")
    registry.transition(rec.id, registry.State.VALIDATED)
    registry.transition(rec.id, registry.State.DEPLOYED_PAUSED)
    registry.allocate(rec.id, 1_000_000, square_off_on_pause=False)
    registry.transition(rec.id, registry.State.RUNNING)
    return registry.get(rec.id)


def _bar(day, hour, minute=0):
    return Bar(datetime(2026, 7, day, hour, minute), 22000, 22010, 21990, 22000)


def test_pnl_persists_at_eod_then_resets_next_day(db):
    rec = _running_record()
    ctx = PaperContext(rec, "NIFTY", MarketHub(SyntheticStore()), 5)

    # --- Day 1: trade, close, persist end-of-day -------------------------
    ctx.push_bar(_bar(16, 10))
    ctx.enter([LegSpec(OptionType.CALL, Action.SELL, strike_offset=0,
                       expiry_kind=ExpiryKind.WEEKLY, lots=1, tag="ce")],
              tag="pbk", sl_pct=0.30)
    ctx.push_bar(_bar(16, 11))
    ctx.exit_all(reason="signal")
    day1_pnl = ctx.day_pnl
    assert day1_pnl != 0.0            # realized something (fees at minimum)
    ctx.persist_day()

    rows = registry.performance_rows(rec.id, "PAPER")
    assert len(rows) == 1 and rows[0]["trade_date"] == "2026-07-16"
    assert rows[0]["realized"] == round(ctx._realized_today, 2)
    day1_equity = rows[0]["equity_eod"]

    # --- Day 2: first bar rolls the counters back to zero ----------------
    ctx.push_bar(_bar(17, 9, 15))
    assert ctx.day_pnl == 0.0        # yesterday's P&L does NOT bleed into today
    assert ctx._realized_today == 0.0 and ctx._fees_today == 0.0
    assert ctx.closed_today == []

    # Day-1 history is untouched and the curve chains from its close
    ctx.push_bar(_bar(17, 15, 25))
    ctx.persist_day()
    rows = registry.performance_rows(rec.id, "PAPER")
    assert [r["trade_date"] for r in rows] == ["2026-07-16", "2026-07-17"]
    assert rows[0]["equity_eod"] == day1_equity          # past day intact
    # day-2 equity chains off day-1 close (flat day → equals it)
    assert rows[1]["equity_eod"] == day1_equity
