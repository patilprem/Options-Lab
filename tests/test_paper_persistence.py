"""M4: paper-position persistence across a simulated restart.

Uses a temp SQLite DB (registry.DB_PATH monkeypatched) and the SyntheticStore
so it runs fully offline.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core import registry
from app.core.contract import (Action, Bar, ExpiryKind, LegSpec, OptionType)
from app.data.store import SyntheticStore
from app.engines.paper import MarketHub, PaperContext, PaperRunner

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "test.db")
    registry.init_db()
    return tmp_path


def _running_record(name="Straddle", code="x"):
    rec = registry.create(name, code)
    registry.transition(rec.id, registry.State.VALIDATED)
    registry.transition(rec.id, registry.State.DEPLOYED_PAUSED)
    registry.allocate(rec.id, 1_000_000, square_off_on_pause=False)
    registry.transition(rec.id, registry.State.RUNNING)
    return registry.get(rec.id)


def _today_ist(hour=10):
    return datetime.now(IST).replace(hour=hour, minute=0, second=0,
                                     microsecond=0, tzinfo=None)


def _enter_straddle(ctx):
    ctx.push_bar(Bar(_today_ist(), 22000, 22010, 21990, 22000))
    ok = ctx.enter([
        LegSpec(OptionType.CALL, Action.SELL, strike_offset=0,
                expiry_kind=ExpiryKind.WEEKLY, lots=1, tag="ce"),
        LegSpec(OptionType.PUT, Action.SELL, strike_offset=0,
                expiry_kind=ExpiryKind.WEEKLY, lots=1, tag="pe"),
    ], tag="straddle", sl_pct=0.30)
    assert ok, "entry should succeed with 1M capital"
    return ctx


def test_persist_on_fill_and_restore(db):
    rec = _running_record()
    hub = MarketHub(SyntheticStore())

    ctx = _enter_straddle(PaperContext(rec, "NIFTY", hub, 5))
    assert len(ctx.positions) == 2
    margin, realized, fees = ctx._margin_used, ctx._realized_today, ctx._fees_today
    assert margin > 0

    # persisted automatically on the fill
    snap = registry.load_paper_state(rec.id)
    assert snap and snap["date"] == datetime.now(IST).date().isoformat()
    assert len(snap["positions"]) == 2

    # --- simulate a process restart: brand-new context, restore from SQLite ---
    ctx2 = PaperContext(rec, "NIFTY", hub, 5)
    assert not ctx2.positions            # starts empty
    n = ctx2.restore_state()
    assert n == 2
    assert ctx2._margin_used == margin
    assert ctx2._realized_today == realized and ctx2._fees_today == fees

    # positions round-tripped faithfully (ids, strikes, signed qty, sl)
    orig = {p.id: p for p in ctx.positions}
    for p in ctx2.positions:
        o = orig[p.id]
        assert (p.strike, p.qty, p.entry_price, p.tag) == (o.strike, o.qty, o.entry_price, o.tag)
        assert p.leg.option_type == o.leg.option_type and p.stop_loss == o.stop_loss
        assert p.expiry == o.expiry and p.entry_ts == o.entry_ts


def test_close_updates_persisted_state(db):
    rec = _running_record()
    hub = MarketHub(SyntheticStore())
    ctx = _enter_straddle(PaperContext(rec, "NIFTY", hub, 5))

    ctx.exit(ctx.positions[0].id)        # close one leg
    snap = registry.load_paper_state(rec.id)
    assert len(snap["positions"]) == 1   # one open leg remains persisted

    ctx.exit_all()
    assert registry.load_paper_state(rec.id)["positions"] == []


def test_persist_day_is_idempotent(db):
    """persist_day runs on EVERY bar after 15:25 and again on stop. A repeat
    call must re-write the same full-day totals, never zero them out."""
    rec = _running_record()
    hub = MarketHub(SyntheticStore())
    ctx = _enter_straddle(PaperContext(rec, "NIFTY", hub, 5))
    ctx.exit_all()                              # realize some P&L
    realized = ctx._realized_today
    assert realized != 0.0

    ctx.persist_day()
    rows = registry.performance_rows(rec.id, "PAPER")
    assert len(rows) == 1 and rows[0]["realized"] == round(realized, 2)

    ctx.persist_day()                           # called again (day-end + stop)
    rows = registry.performance_rows(rec.id, "PAPER")
    assert len(rows) == 1
    assert rows[0]["realized"] == round(realized, 2)   # NOT clobbered to 0
    assert ctx._realized_today == realized             # counters untouched


def test_counters_roll_on_new_trading_day(db):
    rec = _running_record()
    ctx = PaperContext(rec, "NIFTY", MarketHub(SyntheticStore()), 5)
    ctx.push_bar(Bar(_today_ist(), 22000, 22010, 21990, 22000))
    ctx._realized_today = 5000.0
    ctx._fees_today = 100.0

    next_day = _today_ist() + timedelta(days=1)
    ctx.push_bar(Bar(next_day, 22000, 22010, 21990, 22000))
    assert ctx._realized_today == 0.0 and ctx._fees_today == 0.0   # rolled


def test_stale_day_snapshot_is_discarded(db):
    rec = _running_record()
    registry.save_paper_state(rec.id, {
        "date": "2020-01-01", "margin_used": 5000, "realized_today": 0,
        "fees_today": 0, "positions": [{"id": "old"}]})
    ctx = PaperContext(rec, "NIFTY", MarketHub(SyntheticStore()), 5)
    assert ctx.restore_state() == 0                 # different day -> not restored
    assert registry.load_paper_state(rec.id) is None  # and cleared


def test_restore_all_redeploys_and_recovers(db):
    """End-to-end: a strategy left RUNNING with an open position is recovered by
    PaperRunner.restore_all on startup."""
    from app.core.loader import load_strategy_class
    code = Path("examples/short_straddle_920.py").read_text()
    rec = registry.create("Straddle920", code)
    registry.transition(rec.id, registry.State.VALIDATED)
    registry.transition(rec.id, registry.State.DEPLOYED_PAUSED)
    registry.allocate(rec.id, 1_000_000)
    registry.transition(rec.id, registry.State.RUNNING)
    rec = registry.get(rec.id)   # refresh: state is now RUNNING (not paused)

    # First context enters a straddle and persists (the pre-crash state).
    hub = MarketHub(SyntheticStore())
    _enter_straddle(PaperContext(rec, "NIFTY", hub, 5))
    assert len(registry.load_paper_state(rec.id)["positions"]) == 2

    async def scenario():
        runner = PaperRunner(MarketHub(SyntheticStore()))
        runner.hub._use_synthetic = lambda: False   # pretend real data is available
        n = await runner.restore_all(lambda r: load_strategy_class(r.code)())
        assert n == 1
        ctx = runner.contexts[rec.id]
        assert len(ctx.positions) == 2   # recovered
        await runner.stop(rec.id)        # cleanup tasks
        if runner._hb_task:
            runner._hb_task.cancel()

    asyncio.run(scenario())
    # 'recovered 2 open positions' event was logged
    evs = registry.events_for(datetime.now(IST).date().isoformat())
    assert any("recovered 2 open positions" in e["message"] for e in evs)


def test_restore_all_skipped_when_store_is_synthetic(db):
    """The critical guard: a RUNNING strategy must NOT auto-restore-and-trade
    when the store is synthetic (real data unavailable) — that was writing
    fake fills into the real PAPER ledger on every server restart."""
    rec = _running_record()

    async def scenario():
        runner = PaperRunner(MarketHub(SyntheticStore()))   # synthetic -> guard
        n = await runner.restore_all(lambda r: r)
        assert n == 0
        assert rec.id not in runner.contexts               # nothing deployed

    asyncio.run(scenario())
    evs = registry.events_for(datetime.now(IST).date().isoformat())
    assert any("restore skipped" in e["message"] for e in evs)
