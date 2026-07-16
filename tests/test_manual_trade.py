"""Manual trade booking (POST /strategies/{sid}/manual_trade) — offline.

The wipe_day companion: after erasing a day filled off frozen quotes, the
actual fills are re-booked by hand. Fees must come from the shared cost
model (engines/fills.py) and realized must be net of fees, so the manual
row is indistinguishable in shape from an engine-produced one.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.strategies import (ManualLegReq, ManualTradeReq, manual_trade,
                                wipe_day, recompute_equity)
from app.core import registry
from app.core.contract import Action
from app.engines import fills as F


@pytest.fixture
def rec(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "test.db")
    registry.init_db()
    r = registry.create("PBK", "x")
    registry.allocate(r.id, 500_000)
    return registry.get(r.id)


def _straddle_req(day="2026-07-13", update_daily=True):
    return ManualTradeReq(
        day=day, underlying="NIFTY", expiry="2026-07-14", update_daily=update_daily,
        legs=[
            ManualLegReq(option_type="CE", action="SELL", strike=25100, qty=75,
                         entry_price=112.50, exit_price=84.20,
                         entry_ts="09:47", exit_ts="14:32", exit_reason="target"),
            ManualLegReq(option_type="PE", action="SELL", strike=25100, qty=75,
                         entry_price=98.30, exit_price=121.65,
                         entry_ts="09:47", exit_ts="14:32", exit_reason="target"),
        ])


def _expected_leg(entry, exit_, qty, side):
    e = F.charges(entry * qty, Action[side], F.FeeConfig())
    x = F.charges(exit_ * qty, Action["BUY" if side == "SELL" else "SELL"],
                  F.FeeConfig())
    signed = qty if side == "BUY" else -qty
    return (exit_ - entry) * signed - (e + x), e + x


def test_books_blotter_rows_and_daily_pnl(rec):
    res = manual_trade(rec.id, _straddle_req())

    rows = registry.trades_for(rec.id, "2026-07-13")
    assert len(rows) == 4                               # entry + exit per leg
    entry_ce = next(r for r in rows if r["reason"] == "entry" and "CE" in r["contract"])
    assert entry_ce["contract"] == "NIFTY 14JUL26 25100 CE"
    assert entry_ce["ts"] == "2026-07-13 09:47:00"
    assert entry_ce["side"] == "SELL" and entry_ce["price"] == 112.50
    exit_ce = next(r for r in rows if r["reason"] == "target" and "CE" in r["contract"])
    assert exit_ce["side"] == "BUY" and exit_ce["price"] == 84.20

    ce_pnl, ce_fees = _expected_leg(112.50, 84.20, 75, "SELL")
    pe_pnl, pe_fees = _expected_leg(98.30, 121.65, 75, "SELL")
    assert res["realized"] == round(ce_pnl + pe_pnl, 2)
    assert res["fees"] == round(ce_fees + pe_fees, 2)

    perf = registry.paper_performance(rec.id)
    assert len(perf) == 1
    day = perf[0]
    assert day["trade_date"] == "2026-07-13"
    assert day["realized"] == res["realized"]
    assert day["fees"] == res["fees"]
    assert day["equity_eod"] == round(500_000 + res["realized"], 2)


def test_equity_chains_from_prior_day(rec):
    registry.save_paper_day(rec.id, "2026-07-10", 1500.0, 0.0, 200.0, 501_500.0)
    res = manual_trade(rec.id, _straddle_req())
    day = [r for r in registry.paper_performance(rec.id)
           if r["trade_date"] == "2026-07-13"][0]
    assert day["equity_eod"] == round(501_500.0 + res["realized"], 2)


def test_update_daily_false_leaves_pnl_alone(rec):
    manual_trade(rec.id, _straddle_req(update_daily=False))
    assert registry.paper_performance(rec.id) == []
    assert len(registry.trades_for(rec.id, "2026-07-13")) == 4


def test_wipe_then_rebook_round_trip(rec):
    """The incident flow: bad-quote day wiped, then re-booked at real prices."""
    manual_trade(rec.id, _straddle_req())               # the "bad" day
    wipe_day(rec.id, "2026-07-13")
    assert registry.trades_for(rec.id, "2026-07-13") == []
    assert registry.paper_performance(rec.id) == []
    res = manual_trade(rec.id, _straddle_req())         # re-book actuals
    assert len(registry.trades_for(rec.id, "2026-07-13")) == 4
    assert registry.paper_performance(rec.id)[0]["realized"] == res["realized"]


def test_manual_trade_cascades_equity_to_later_days(rec):
    """A later day's equity_eod was chained off day X's OLD close — re-booking
    day X with different (real) fills must refresh every day after it too,
    not just X itself."""
    manual_trade(rec.id, _straddle_req())                       # 2026-07-13
    day13_old_equity = registry.paper_performance(rec.id)[0]["equity_eod"]
    # a flat day that chained off the (soon to be stale) 07-13 close
    registry.save_paper_day(rec.id, "2026-07-14", 0.0, 0.0, 0.0, day13_old_equity)

    wipe_day(rec.id, "2026-07-13")
    req = _straddle_req()
    req.legs[0].exit_price = 60.0                                # different real fill
    res = manual_trade(rec.id, req)

    perf = {r["trade_date"]: r for r in registry.paper_performance(rec.id)}
    assert perf["2026-07-13"]["equity_eod"] == res["daily_row"]["equity_eod"]
    assert perf["2026-07-13"]["equity_eod"] != day13_old_equity
    assert perf["2026-07-14"]["equity_eod"] == perf["2026-07-13"]["equity_eod"]


def test_wipe_day_cascades_equity_to_later_days(rec):
    """Wiping a day removes it entirely — any later day must re-chain from
    whatever now precedes it (or allocated_capital, if nothing does)."""
    manual_trade(rec.id, _straddle_req())                        # 2026-07-13
    day13 = registry.paper_performance(rec.id)[0]
    registry.save_paper_day(rec.id, "2026-07-14", 0.0, 0.0, 0.0, day13["equity_eod"])

    wipe_day(rec.id, "2026-07-13")

    day14 = [r for r in registry.paper_performance(rec.id)
             if r["trade_date"] == "2026-07-14"][0]
    assert day14["equity_eod"] == 500_000.0


def test_recompute_equity_repairs_stale_chain(rec):
    """Drift left over from before the cascade fix existed: rebuild the whole
    ledger (from_date omitted) and every day's equity_eod re-chains from
    allocated_capital using its own already-stored realized/unrealized."""
    registry.save_paper_day(rec.id, "2026-07-13", -1206.0, 0.0, 52.0, 498794.0)
    registry.save_paper_day(rec.id, "2026-07-14", 0.0, 0.0, 0.0, 500_000.0)  # stale
    registry.save_paper_day(rec.id, "2026-07-15", -1087.0, 0.0, 66.0, 498913.0)  # stale

    res = recompute_equity(rec.id)
    assert res["rows_updated"] == 3

    perf = {r["trade_date"]: r["equity_eod"] for r in registry.paper_performance(rec.id)}
    assert perf["2026-07-13"] == 498794.0
    assert perf["2026-07-14"] == 498794.0
    assert perf["2026-07-15"] == round(498794.0 - 1087.0, 2)


def test_validation_errors(rec):
    req = _straddle_req()
    req.legs[0].action = "HOLD"
    with pytest.raises(HTTPException):
        manual_trade(rec.id, req)
    req = _straddle_req()
    req.legs[0].entry_ts = "9am"
    with pytest.raises(HTTPException):
        manual_trade(rec.id, req)
    req = _straddle_req(day="13-07-2026")
    with pytest.raises(HTTPException):
        manual_trade(rec.id, req)
