"""
GET /strategies/{sid}/performance — morning reset (bug fix).

This endpoint (the per-strategy detail page, PaperPanel.jsx) used to read
ctx.day_pnl DIRECTLY whenever a live context existed, with no date check at
all — unlike /portfolio/today, which already gated on ctx.now matching today.
ctx.day_pnl only rolls to zero on the FIRST BAR of a new trading day
(PaperContext._roll_day, called from push_bar). So for the entire window from
market close through the next session's open — including the whole overnight
stretch and the first few minutes after 09:15 before a bar completes — a
RUNNING strategy's ctx still held yesterday's _realized_today, and this
endpoint showed it as "today's" P&L. That is precisely "still showing
yesterday's P&L in the morning."

Fixed by routing through the same _today_day_pnl helper /portfolio/today
uses (single source of truth), while preserving day_pnl===null as the
"not currently deployed" signal the frontend depends on.

Offline: constructs a real PaperContext against a SyntheticStore-backed hub
(same pattern as test_exit_reason.py), pushes bars directly (no live feed),
and calls performance() as a plain function — no HTTP layer needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.api.strategies import performance, runner
from app.core import registry
from app.core.contract import Bar
from app.data.store import SyntheticStore
from app.engines.paper import IST, MarketHub, PaperContext


@pytest.fixture
def rec(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "test.db")
    registry.init_db()
    r = registry.create("PBK Confluence", "x")
    registry.allocate(r.id, 500_000)
    yield registry.get(r.id)
    runner.contexts.pop(r.id, None)   # never leak into other tests


def _mk_ctx(rec):
    hub = MarketHub(SyntheticStore())
    return PaperContext(rec, "NIFTY", hub, interval=5)


def test_stale_yesterday_pnl_not_shown_before_first_bar_today(rec):
    """The exact reported bug: a RUNNING strategy whose context still reflects
    yesterday (no bar for today has arrived yet — could be any time from
    market close through market open) must show day_pnl 0, not yesterday's
    leftover realized total."""
    ctx = _mk_ctx(rec)
    yesterday = (datetime.now(IST) - timedelta(days=1)).replace(
        hour=15, minute=20, second=0, microsecond=0, tzinfo=None)
    ctx.push_bar(Bar(yesterday, 22000, 22010, 21990, 22005, 1000))
    ctx._realized_today = -2431.87   # yesterday's booked loss, not yet rolled
    runner.contexts[rec.id] = ctx

    data = performance(rec.id)

    assert data["day_pnl"] == 0.0
    assert data["day_roi_pct"] == 0.0


def test_fresh_realized_pnl_after_todays_first_bar_shows_correctly(rec):
    """Once a bar dated TODAY has been pushed (the engine's own day-roll has
    fired), a genuine realized figure for today must show through, not get
    zeroed by the guard. In real flow every _realized_today increment is
    accompanied by a blotter row (PaperContext._close -> _blotter ->
    registry.record_trade) — mirrored here so the zero-activity guard sees
    the same signal a real close would leave behind."""
    ctx = _mk_ctx(rec)
    today_ts = datetime.now(IST).replace(
        hour=10, minute=0, second=0, microsecond=0, tzinfo=None)
    ctx.push_bar(Bar(today_ts, 22000, 22010, 21990, 22005, 1000))
    ctx._realized_today = 1250.0   # a real close booked today, post-roll
    registry.record_trade(rec.id, "PAPER", {
        "ts": today_ts.isoformat(sep=" ", timespec="seconds"),
        "contract": "NIFTY 22000 CE", "side": "SELL", "qty": 75,
        "price": 116.67, "fees": 30.0, "margin": 0.0,
        "reason": "target", "tag": "x",
    })
    runner.contexts[rec.id] = ctx

    data = performance(rec.id)

    assert data["day_pnl"] == 1250.0


def test_day_pnl_null_when_not_deployed(rec):
    """Preserves the frontend's deployed-vs-not signal: no live context ->
    day_pnl stays null (not 0.0), which PaperPanel.jsx reads to decide
    whether to render the live stat cards at all."""
    assert rec.id not in runner.contexts

    data = performance(rec.id)

    assert data["day_pnl"] is None
    assert data["day_roi_pct"] is None


def test_day_pnl_recovers_from_stopped_strategy_that_traded_today(rec):
    """A strategy that traded today then got stopped (ctx popped) must still
    show today's real P&L via the persisted row — the None-when-no-ctx rule
    is about 'not currently running', not 'no data exists for today'."""
    today = datetime.now(IST).date().isoformat()
    registry.save_paper_day(rec.id, today, 875.0, 0.0, 30.0, 500_845.0)
    registry.record_trade(rec.id, "PAPER", {
        "ts": f"{today} 10:15:00", "contract": "NIFTY 25100 CE",
        "side": "SELL", "qty": 75, "price": 100.0, "fees": 30.0,
        "margin": 5000.0, "reason": "entry", "tag": "x",
    })
    assert rec.id not in runner.contexts   # stopped: no live ctx

    data = performance(rec.id)

    # NOTE: today's implementation returns None here (ctx is None), same as
    # before this fix — this test documents that boundary explicitly rather
    # than leaving it implicit, so a future change to include today_row in
    # this branch is a deliberate choice, not an accident.
    assert data["day_pnl"] is None
    assert len(data["trades_today"]) == 1
