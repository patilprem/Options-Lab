"""M8: live execution adapter — verified against a recording DRY-RUN client.
NO real orders are placed (also impossible without the static IP). Confirms the
safety gates, super-order mapping, LIVE-ledger separation, and kill switch."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.core import registry
from app.core.contract import (Action, Bar, ExpiryKind, LegSpec, OptionQuote, OptionType)
from app.engines import live as L

FRI_10AM = datetime(2026, 7, 10, 10, 0)      # a weekday, in market hours
SAT = datetime(2026, 7, 11, 10, 0)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "l.db")
    registry.init_db()
    return tmp_path


class FakeHub:
    def __init__(self, q):
        self._q = q
    def quote(self, u, ts, leg):
        return self._q
    def quote_position(self, u, ts, pos):   # fixed-strike marking (same canned quote)
        return self._q
    def market_client(self):
        return None
    def register(self, *a):
        pass
    def subscribe(self):
        import asyncio
        return asyncio.Queue()
    async def ensure_started(self):
        pass


def _quote(otype=OptionType.CALL, sid="51366"):
    return OptionQuote(ts=FRI_10AM, underlying="NIFTY", expiry=date(2026, 7, 16),
                       strike=23950.0, option_type=otype, ltp=145.0, bid=145.0,
                       ask=146.0, security_id=sid)


def _running_rec(name="Live", cap=3_000_000):
    rec = registry.create(name, "code")
    registry.transition(rec.id, registry.State.VALIDATED)
    registry.transition(rec.id, registry.State.DEPLOYED_PAUSED)
    registry.allocate(rec.id, cap)
    registry.transition(rec.id, registry.State.RUNNING)
    return registry.get(rec.id)


def _ctx(db_rec, client):
    ctx = L.LiveContext(db_rec, "NIFTY", FakeHub(_quote()), 5, client=client)
    ctx.push_bar(Bar(FRI_10AM, 23950, 23960, 23940, 23950))
    return ctx


LEGS = [LegSpec(OptionType.CALL, Action.SELL, 0, ExpiryKind.WEEKLY, 0, 1, "ce"),
        LegSpec(OptionType.PUT, Action.SELL, 0, ExpiryKind.WEEKLY, 0, 1, "pe")]


# --- enter -> super orders + LIVE ledger ------------------------------------

def test_enter_places_super_orders_and_writes_live_ledger(db):
    registry.set_setting("live_enabled", "on")
    client = L.DryRunOrderClient()
    ctx = _ctx(_running_rec(), client)

    assert ctx.enter(LEGS, tag="straddle", sl_pct=0.30) is True
    supers = [o for o in client.orders if o["kind"] == "super"]
    assert len(supers) == 2
    for o in supers:
        assert o["transaction_type"] == "SELL"
        assert o["security_id"] == "51366"
        assert o["product_type"] == "INTRADAY"
        assert o["stopLossPrice"] > o["price"]        # short-leg SL sits ABOVE entry
    # LIVE ledger (never PAPER)
    live_trades = registry.all_trades(mode="LIVE")
    assert len(live_trades) == 2 and all(t["dry_run"] for t in live_trades)
    assert registry.all_trades(mode="PAPER") == []


# --- gates ------------------------------------------------------------------

def test_gate_blocks_when_master_switch_off(db):
    # live_enabled defaults off
    client = L.DryRunOrderClient()
    assert _ctx(_running_rec(), client).enter(LEGS, sl_pct=0.3) is False
    assert client.orders == []


def test_gate_blocks_outside_market_hours(db):
    registry.set_setting("live_enabled", "on")
    client = L.DryRunOrderClient()
    ctx = L.LiveContext(_running_rec(), "NIFTY", FakeHub(_quote()), 5, client=client)
    ctx.push_bar(Bar(SAT, 23950, 23960, 23940, 23950))    # weekend
    assert ctx.enter(LEGS, sl_pct=0.3) is False
    assert client.orders == []


def test_gate_blocks_over_max_lots(db):
    registry.set_setting("live_enabled", "on")
    registry.set_setting("live_max_lots", "1")
    client = L.DryRunOrderClient()
    big = [LegSpec(OptionType.CALL, Action.SELL, 0, ExpiryKind.WEEKLY, 0, 5, "ce")]
    assert _ctx(_running_rec(), client).enter(big, sl_pct=0.3) is False
    assert client.orders == []


def test_gate_blocks_when_kill_armed(db):
    registry.set_setting("live_enabled", "on")
    registry.set_setting("live_kill_armed", "yes")
    client = L.DryRunOrderClient()
    assert _ctx(_running_rec(), client).enter(LEGS, sl_pct=0.3) is False


# --- client selection (no real orders without explicit opt-in) --------------

def test_make_order_client_is_dryrun_by_default(db):
    registry.set_setting("live_enabled", "on")          # enabled but dry_run defaults on
    assert isinstance(L.make_order_client(), L.DryRunOrderClient)
    # even with dry_run off, no creds here -> still DryRun (never a live client)
    registry.set_setting("live_dry_run", "off")
    assert isinstance(L.make_order_client(), L.DryRunOrderClient)


# --- exit + kill switch -----------------------------------------------------

def test_exit_all_squares_positions(db):
    registry.set_setting("live_enabled", "on")
    client = L.DryRunOrderClient()
    ctx = _ctx(_running_rec(), client)
    ctx.enter(LEGS, sl_pct=0.3)
    ctx.exit_all()
    assert ctx.positions == []
    assert any(o["kind"] == "cancel" for o in client.orders)
    assert any(o["kind"] == "order" for o in client.orders)   # squaring orders


def test_kill_switch_arm_disarm(db):
    client = L.DryRunOrderClient()
    ks = L.KillSwitch(client)
    ks.arm()
    assert ks.armed() is True
    assert client.orders[-1] == {"kind": "kill_switch", "action": "activate"}
    ks.disarm()
    assert ks.armed() is False


def test_runner_portfolio_breach_kills_all(db):
    registry.set_setting("live_enabled", "on")
    registry.set_setting("risk_max_daily_loss", "8000")
    runner = L.LiveRunner(FakeHub(_quote()))
    for name, loss in (("A", -5000), ("B", -4000)):
        client = L.DryRunOrderClient()
        ctx = _ctx(_running_rec(name), client)
        ctx._realized_today = loss
        runner.contexts[ctx.rec.id] = ctx

    runner.enforce_risk()
    assert all(c.paused for c in runner.contexts.values())
    assert registry.setting("live_kill_armed", "no") == "yes"   # broker kill armed
