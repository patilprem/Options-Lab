"""M7: risk panel — pure evaluate/exposure/snapshot + PaperRunner enforcement.
Offline (temp DB, SyntheticStore)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace as NS

import pytest

from app.core import registry
from app.data.store import SyntheticStore
from app.engines import risk as R
from app.engines.paper import MarketHub, PaperContext, PaperRunner


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "r.db")
    registry.init_db()
    return tmp_path


def _ctx(sid, day_pnl, margin=0.0, positions=(), name="S", state="RUNNING", paused=False):
    rec = NS(id=sid, name=name, allocated_capital=1_000_000,
             state=NS(value=state))
    return NS(rec=rec, day_pnl=day_pnl, _margin_used=margin,
              positions=list(positions), paused=paused)


# --- evaluate (pure decisions) ----------------------------------------------

def test_evaluate_per_strategy_cap(db):
    registry.set_setting("risk_default_loss_cap", "5000")
    contexts = {"a": _ctx("a", -6000), "b": _ctx("b", -3000)}
    ev = R.evaluate(contexts)
    assert ev["portfolio_day_pnl"] == -9000
    assert [b["sid"] for b in ev["strategy_breaches"]] == ["a"]   # only 'a' over cap
    assert ev["portfolio_breach"] is False                        # no portfolio limit set


def test_evaluate_portfolio_breach(db):
    registry.set_setting("risk_max_daily_loss", "10000")
    contexts = {"a": _ctx("a", -6000), "b": _ctx("b", -5000)}
    ev = R.evaluate(contexts)
    assert ev["portfolio_breach"] is True   # -11000 <= -10000
    contexts["b"] = _ctx("b", -1000)
    assert R.evaluate(contexts)["portfolio_breach"] is False


def test_per_strategy_override_beats_default(db):
    registry.set_setting("risk_default_loss_cap", "5000")
    registry.set_setting("risk_loss_cap:a", "20000")
    assert R.loss_cap_for("a") == 20000
    assert R.loss_cap_for("b") == 5000


# --- exposure ---------------------------------------------------------------

def test_exposure_groups_by_underlying_and_expiry(db):
    e1, e2 = date(2026, 7, 14), date(2026, 7, 21)
    pos = [
        NS(underlying="NIFTY", expiry=e1, qty=-75, mtm_price=120.0),
        NS(underlying="NIFTY", expiry=e1, qty=-75, mtm_price=100.0),
        NS(underlying="BANKNIFTY", expiry=e2, qty=35, mtm_price=200.0),
    ]
    exp = R.exposure({"a": _ctx("a", 0, positions=pos)})
    by_u = {r["underlying"]: r for r in exp["by_underlying"]}
    assert by_u["NIFTY"]["positions"] == 2 and by_u["NIFTY"]["net_qty"] == -150
    assert by_u["NIFTY"]["premium"] == 75 * 120 + 75 * 100
    by_e = {r["expiry"]: r for r in exp["by_expiry"]}
    assert by_e["2026-07-14"]["positions"] == 2
    assert by_e["2026-07-21"]["net_qty"] == 35


# --- snapshot ---------------------------------------------------------------

def test_snapshot_shape_and_margin_util(db):
    registry.set_setting("risk_max_daily_loss", "20000")
    contexts = {"a": _ctx("a", -5000, margin=250_000)}
    snap = R.snapshot(contexts)
    assert snap["portfolio"]["allocated"] == 1_000_000
    assert snap["portfolio"]["margin_util_pct"] == 25.0
    assert snap["portfolio"]["loss_used_pct"] == 25.0   # 5000 / 20000
    assert snap["strategies"][0]["id"] == "a"
    assert "exposure_by_underlying" in snap and "settings" in snap


# --- PaperRunner enforcement (integration) ----------------------------------

def _running(sid_name):
    rec = registry.create(sid_name, "code")
    registry.transition(rec.id, registry.State.VALIDATED)
    registry.transition(rec.id, registry.State.DEPLOYED_PAUSED)
    registry.allocate(rec.id, 1_000_000)
    registry.transition(rec.id, registry.State.RUNNING)
    return registry.get(rec.id)


def test_enforce_pauses_strategy_over_cap(db):
    rec = _running("A")
    registry.set_setting("risk_default_loss_cap", "5000")
    runner = PaperRunner(MarketHub(SyntheticStore()))
    ctx = PaperContext(rec, "NIFTY", runner.hub, 5)
    ctx._realized_today = -6000            # simulate a losing day
    runner.contexts[rec.id] = ctx

    runner.enforce_risk()
    assert ctx.paused is True
    assert registry.get(rec.id).state == registry.State.DEPLOYED_PAUSED
    # a risk event was recorded
    from datetime import datetime, timezone, timedelta
    day = datetime.now(timezone(timedelta(hours=5, minutes=30))).date().isoformat()
    evs = registry.events_for(day)
    assert any(e["kind"] == "risk" and "loss cap" in e["message"] for e in evs)


def test_enforce_portfolio_breach_pauses_all(db):
    registry.set_setting("risk_max_daily_loss", "8000")
    runner = PaperRunner(MarketHub(SyntheticStore()))
    ctxs = []
    for name, loss in (("A", -5000), ("B", -4000)):
        rec = _running(name)
        ctx = PaperContext(rec, "NIFTY", runner.hub, 5)
        ctx._realized_today = loss
        runner.contexts[rec.id] = ctx
        ctxs.append((rec, ctx))

    runner.enforce_risk()
    assert all(ctx.paused for _, ctx in ctxs)           # -9000 <= -8000 -> all paused
    assert all(registry.get(rec.id).state == registry.State.DEPLOYED_PAUSED
               for rec, _ in ctxs)
