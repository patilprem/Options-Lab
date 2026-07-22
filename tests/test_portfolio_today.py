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

from app.api.strategies import portfolio_today, runner, scanner_engine
from app.core import registry
from app.core.contract import Bar, LegSpec, OptionType, Action, Position
from app.data.store import SyntheticStore
from app.engines.paper import IST, MarketHub, PaperContext
from app.engines.scanner_trader import SPosition, STRATEGY_ID as SCANNER_ID


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
    # the underlying/company name must be on the row — the frontend has
    # nothing to render a "Contract" column with otherwise
    assert data["open_positions"][0]["symbol"] == "NIFTY"


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


# --- scanner auto-trader joining the portfolio totals -----------------------

@pytest.fixture
def scanner_db(tmp_path, monkeypatch):
    """Isolated DB for scanner-integration tests; always cleans up the
    module-level scanner_engine's book so nothing leaks into later tests."""
    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "test_scanner.db")
    registry.init_db()
    yield
    scanner_engine.trader.book.clear()


def test_scanner_off_contributes_nothing(scanner_db):
    """scanner_trade defaults to off — the scanner must not appear anywhere
    in the portfolio, not even as a zero row."""
    data = portfolio_today()
    assert not any(s["id"] == SCANNER_ID for s in data["strategies"])
    assert not any(p.get("strategy_id") == SCANNER_ID for p in data["open_positions"])
    assert data["totals"]["allocated_capital"] == 0.0


def test_scanner_on_joins_totals_positions_and_trades(scanner_db):
    """With scanner_trade on, its capital/day P&L join the same totals a
    Strategy's do, and its open position + today's fill show up in the
    unified positions/trades lists (no more per-strategy-only view)."""
    today = _today()
    registry.set_setting("scanner_trade", "on")
    registry.set_setting("scanner_trade_capital", "500000")
    registry.save_paper_day(SCANNER_ID, today, 2000.0, 0.0, 60.0, 502_000.0)
    registry.record_trade(SCANNER_ID, "PAPER", {
        "ts": f"{today} 10:30:00", "contract": "RELIANCE 1250 CALL",
        "side": "BUY", "qty": 500, "price": 20.0, "fees": 30.0,
        "margin": 0.0, "reason": "entry", "tag": "scanner:CE",
    })
    scanner_engine.trader.book["RELIANCE"] = SPosition(
        symbol="RELIANCE", bias="CE", side="CALL", strike=1250, lots=1,
        qty_units=500, entry_price=20.0, entry_fees=30.0,
        entry_ts=f"{today}T10:30:00", entry_score=80.0,
        high_water=22.0, mtm=22.0, low_water=20.0,
        entry_ctx={"expiry": "2026-07-31"})

    data = portfolio_today()

    scan = next(s for s in data["strategies"] if s["id"] == SCANNER_ID)
    assert scan["allocated_capital"] == 500_000.0
    assert scan["open_positions"] == 1
    # day_pnl = booked realized (2000) + open unrealized ((22-20)*500 = 1000)
    assert scan["day_pnl"] == pytest.approx(3000.0)
    assert any(p["strategy_id"] == SCANNER_ID for p in data["open_positions"])
    assert any(t["strategy_id"] == SCANNER_ID for t in data["trades_today"])
    # the underlying/company name (RELIANCE) must be on the row — the
    # frontend has nothing to render a "Contract" column with otherwise
    scan_pos = next(p for p in data["open_positions"] if p["strategy_id"] == SCANNER_ID)
    assert scan_pos["symbol"] == "RELIANCE"
    assert data["totals"]["allocated_capital"] == pytest.approx(500_000.0)
    # equity includes the scanner's capital + BOOKED realized only — the open
    # position's unrealized swing must not move it (same guard as Strategies)
    assert data["totals"]["equity"] == pytest.approx(500_000.0 + 2000.0)
    assert data["totals"]["growth"] == pytest.approx(2000.0)


def test_scanner_stale_row_zeroed_when_no_activity_today(scanner_db):
    """Same stale-row guard as Strategies: a leftover daily_pnl row for today
    with nothing open and nothing traded must read as zero, not the stale
    value — otherwise a scanner that was on yesterday and toggled off (or
    just idle) would show a phantom P&L forever."""
    registry.set_setting("scanner_trade", "on")
    registry.save_paper_day(SCANNER_ID, _today(), -999.0, 0.0, 0.0, 499_001.0)

    data = portfolio_today()

    scan = next(s for s in data["strategies"] if s["id"] == SCANNER_ID)
    assert scan["day_pnl"] == 0.0


def test_scanner_position_carries_overnight_into_a_new_day(scanner_db):
    """The scanner is positional (see exit_decision's max_hold_days, not an
    intraday square-off) — a position opened yesterday and still open today
    must (a) keep showing in open_positions, (b) keep its live mark-to-market
    reflected in TODAY's P&L card even though it wasn't entered today, and
    (c) NOT lose yesterday's already-booked realized P&L, which must still
    count permanently toward equity — while today's OWN daily_pnl row starts
    fresh at zero with no separate reset logic needed (no row exists for
    today yet)."""
    from datetime import timedelta
    today = _today()
    yesterday = (datetime.now(IST).date() - timedelta(days=1)).isoformat()
    registry.set_setting("scanner_trade", "on")
    registry.set_setting("scanner_trade_capital", "500000")
    # yesterday's day already closed out with a booked win — must never vanish
    registry.save_paper_day(SCANNER_ID, yesterday, 1500.0, 0.0, 40.0, 501_500.0)
    registry.record_trade(SCANNER_ID, "PAPER", {
        "ts": f"{yesterday} 14:13:00", "contract": "DRREDDY 1170 PUT",
        "side": "BUY", "qty": 625, "price": 19.75, "fees": 29.15,
        "margin": 0.0, "reason": "entry", "tag": "scanner:PE",
    })
    # the position from that entry is STILL OPEN today (never squared off)
    scanner_engine.trader.book["DRREDDY"] = SPosition(
        symbol="DRREDDY", bias="PE", side="PUT", strike=1170, lots=1,
        qty_units=625, entry_price=19.75, entry_fees=29.15,
        entry_ts=f"{yesterday}T14:13:00", entry_score=88.9,
        high_water=22.0, mtm=22.0, low_water=19.75,
        entry_ctx={"expiry": "2026-07-28"})

    data = portfolio_today()

    scan = next(s for s in data["strategies"] if s["id"] == SCANNER_ID)
    # (a) still open, still on the dashboard
    assert scan["open_positions"] == 1
    assert any(p["strategy_id"] == SCANNER_ID and p["strategy"] == "Scanner Auto-Trader"
              for p in data["open_positions"])
    # (b) today's P&L reflects the live mark on the carried position even
    # though today's OWN booked realized is zero (nothing closed today)
    assert scan["day_pnl"] == pytest.approx((22.0 - 19.75) * 625)
    # yesterday's fill does NOT reappear in today's trade list
    assert not any(t["strategy_id"] == SCANNER_ID for t in data["trades_today"])
    # (c) yesterday's booked win is permanent — equity/growth include it,
    # and never the still-open position's unrealized swing
    assert data["totals"]["equity"] == pytest.approx(500_000.0 + 1500.0)
    assert data["totals"]["growth"] == pytest.approx(1500.0)
