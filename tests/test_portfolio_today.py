"""
GET /portfolio/today — zero-activity guard against stale P&L (bug fix).

Reported symptom: the Dashboard's "Today's P&L" showed a nonzero number on a
day with 0 open positions and 0 fills — i.e. yesterday's (or some stale)
daily_pnl row being read as if it were today's, exactly the "Saturday shows
Friday's P&L" bug class already documented (and partly fixed) elsewhere in
this codebase for the paper_state restore path. This is the SAME bug class in
a DIFFERENT code path: the /portfolio/today handler's own today_row fallback.

Offline: calls portfolio_today() directly (not via HTTP) against an isolated
SQLite DB, following the pattern in test_manual_trade.py.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.api.strategies import portfolio_today, runner
from app.core import registry
from app.engines.paper import IST


@pytest.fixture
def rec(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "test.db")
    registry.init_db()
    r = registry.create("PBK Confluence", "x")
    registry.transition(r.id, registry.State.VALIDATED)
    registry.transition(r.id, registry.State.DEPLOYED_PAUSED)
    registry.allocate(r.id, 500_000)
    registry.transition(r.id, registry.State.RUNNING)   # match the real report: RUNNING
    yield registry.get(r.id)
    runner.contexts.pop(r.id, None)   # never leak into other tests


def _today() -> str:
    return datetime.now(IST).date().isoformat()


def test_stale_row_zeroed_when_no_activity_today(rec):
    """A daily_pnl row exists for today (leftover), but nothing traded today
    and nothing is open — day_pnl must read as 0, not the stale value."""
    assert rec.id not in runner.contexts   # no live ctx tracking this strategy
    registry.save_paper_day(rec.id, _today(), -2431.87, 0.0, 0.0, 497_568.13)

    data = portfolio_today()

    strat = next(s for s in data["strategies"] if s["id"] == rec.id)
    assert strat["day_pnl"] == 0.0
    assert strat["open_positions"] == 0 and strat["trades_today"] == 0
    assert data["totals"]["day_pnl"] == 0.0


def test_real_trade_today_is_not_zeroed(rec):
    """The guard must not clobber a strategy that GENUINELY traded today (e.g.
    stopped right after, so ctx is gone but the day's row/trades are real)."""
    today = _today()
    registry.save_paper_day(rec.id, today, 1250.0, 0.0, 45.0, 501_205.0)
    registry.record_trade(rec.id, "PAPER", {
        "ts": f"{today} 10:15:00", "contract": "NIFTY 25100 CE",
        "side": "SELL", "qty": 75, "price": 100.0, "fees": 45.0,
        "margin": 5000.0, "reason": "entry", "tag": "x",
    })

    data = portfolio_today()

    strat = next(s for s in data["strategies"] if s["id"] == rec.id)
    assert strat["day_pnl"] == 1250.0
    assert strat["trades_today"] == 1
    assert data["totals"]["day_pnl"] == 1250.0


def test_date_is_ist_not_naive_local(rec):
    """The 'today' the endpoint reports must match IST wall-clock — the whole
    paper engine (PaperContext._roll_day, _session_date) keys off IST, so a
    naive datetime.now() here would disagree with it near the IST/UTC
    midnight offset on any server not clocked to IST."""
    data = portfolio_today()
    assert data["date"] == datetime.now(IST).date().isoformat()
