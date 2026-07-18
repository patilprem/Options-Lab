"""Exit-attribution regression.

A strategy that closes its own position via ctx.exit()/ctx.exit_all() used to
book the trade with reason="manual" — indistinguishable from a human
intervention (the manual_trade endpoint) on the blotter, poisoning any
exit-attribution analysis. Strategy-initiated exits must now record "signal"
(or whatever reason the strategy passes); "manual" is reserved for humans.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core import registry
from app.core.contract import Action, Bar, ExpiryKind, LegSpec, OptionType
from app.data.store import SyntheticStore
from app.engines.paper import MarketHub, PaperContext

IST = timezone(timedelta(hours=5, minutes=30))


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


def _enter(ctx):
    now = datetime.now(IST).replace(hour=10, minute=0, second=0,
                                    microsecond=0, tzinfo=None)
    ctx.push_bar(Bar(now, 22000, 22010, 21990, 22000))
    ok = ctx.enter([LegSpec(OptionType.CALL, Action.BUY, strike_offset=0,
                            expiry_kind=ExpiryKind.WEEKLY, lots=1, tag="pbk")],
                   tag="pbk", sl_pct=0.30)
    assert ok
    return ctx.now.date().isoformat()


def _exit_reasons(sid, day):
    return [t["reason"] for t in registry.trades_for(sid, day, "PAPER")
            if t["side"] in ("SELL", "BUY") and t["reason"] != "entry"]


def test_strategy_exit_books_signal_not_manual(db):
    rec = _running_record()
    ctx = PaperContext(rec, "NIFTY", MarketHub(SyntheticStore()), 5)
    day = _enter(ctx)

    ctx.exit(ctx.positions[0].id)        # strategy's own decision, no reason

    reasons = _exit_reasons(rec.id, day)
    assert reasons == ["signal"], reasons
    assert "manual" not in reasons       # never mislabel a strategy exit


def test_exit_reason_passes_through(db):
    rec = _running_record()
    ctx = PaperContext(rec, "NIFTY", MarketHub(SyntheticStore()), 5)
    day = _enter(ctx)

    ctx.exit_all(reason="time_exit")     # descriptive reason survives to blotter

    assert _exit_reasons(rec.id, day) == ["time_exit"]
