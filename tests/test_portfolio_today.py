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
from app.core.contract import Bar, LegSpec, OptionType, Action, Position
from app.data.store import SyntheticStore
from app.engines.paper import IST, MarketHub, PaperContext


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


def _open_position(entry=100.0, mtm=250.0) -> Position:
    leg = LegSpec(OptionType.CALL, Action.SELL)
    pos = Position(id="p1", leg=leg, underlying="NIFTY", expiry=None,
                   strike=22000, qty=-75, entry_price=entry,
                   entry_ts=datetime.now(IST).replace(tzinfo=None))
    pos.mark(mtm)   # big unrealized swing, nothing booked yet
    return pos


def test_equity_and_growth_ignore_unrealized_swings(rec):
    """A large live mark-to-market swing on a still-open position must NOT
    move the dashboard's equity/growth totals — those are the BOOKED view
    and change only when a trade actually closes. (Today's P&L card is the
    separate, deliberately-live view and is untouched by this guard.)"""
    hub = MarketHub(SyntheticStore())
    ctx = PaperContext(rec, "NIFTY", hub, interval=5)
    today_ts = datetime.now(IST).replace(hour=10, minute=0, second=0,
                                         microsecond=0, tzinfo=None)
    ctx.push_bar(Bar(today_ts, 22000, 22010, 21990, 22005, 1000))
    ctx._open.append(_open_position())   # ₹11,250 unrealized (short, price ran up)
    runner.contexts[rec.id] = ctx

    data = portfolio_today()

    assert data["totals"]["equity"] == 500_000.0
    assert data["totals"]["growth"] == 0.0
    # the live P&L card is untouched — it still reflects the open mark
    strat = next(s for s in data["strategies"] if s["id"] == rec.id)
    assert strat["day_pnl"] != 0.0


def test_equity_moves_once_a_trade_is_booked(rec):
    """Once P&L is actually realized (a trade closed), equity/growth must
    reflect it immediately — the guard is 'ignore unrealized', not 'ignore
    today'."""
    hub = MarketHub(SyntheticStore())
    ctx = PaperContext(rec, "NIFTY", hub, interval=5)
    today_ts = datetime.now(IST).replace(hour=10, minute=0, second=0,
                                         microsecond=0, tzinfo=None)
    ctx.push_bar(Bar(today_ts, 22000, 22010, 21990, 22005, 1000))
    ctx._realized_today = 1250.0   # a real close, booked today
    runner.contexts[rec.id] = ctx

    data = portfolio_today()

    assert data["totals"]["equity"] == 501_250.0
    assert data["totals"]["growth"] == 1250.0
