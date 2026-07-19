"""Tests for the Strategy trade-journal analytics (strategy_insights.py).

Pure: synthetic closed round-trip rows in -> stats + suggestions out. Each rule
is exercised with data that should fire it, plus minimum-sample gates and a
clean-book case that must suggest nothing.
"""

from __future__ import annotations

from app.engines import strategy_insights as si


def _rt(pnl=1000.0, reason="signal", entry_ts="2026-07-16 10:00",
        mfe=None, mae=None, fees=80.0, tod="10:00", iv=None, held=120):
    ctx = {"tod": tod}
    if iv is not None:
        ctx["iv_rank"] = iv
    return {"pnl": pnl, "exit_reason": reason, "entry_ts": entry_ts,
            "mfe": pnl * 1.5 if mfe is None else mfe,
            "mae": min(pnl, 0.0) if mae is None else mae,
            "fees": fees, "held_minutes": held, "entry_context": ctx}


def _rules(res):
    return {s["rule"] for s in res["suggestions"]}


def test_insufficient_data_only():
    res = si.analyze([_rt(), _rt(pnl=-500)])
    assert res["ready"] is False
    assert _rules(res) == {"insufficient_data"}
    assert res["overall"]["n"] == 2


def test_overall_stats_and_profit_factor():
    rows = [_rt(pnl=1000), _rt(pnl=1000), _rt(pnl=-500), _rt(pnl=-500)] * 2
    res = si.analyze(rows)
    o = res["overall"]
    assert o["n"] == 8 and o["wins"] == 4 and o["losses"] == 4
    assert o["win_rate"] == 0.5 and o["total"] == 2000.0
    assert o["profit_factor"] == 2.0
    assert o["total_fees"] == 8 * 80.0


def test_stops_dominate_losses():
    rows = ([_rt(pnl=-800, reason="stop_loss", mae=-800) for _ in range(5)]
            + [_rt(pnl=300, reason="target") for _ in range(3)])
    res = si.analyze(rows)
    assert "stops_dominate_losses" in _rules(res)


def test_target_too_far_when_winners_give_back():
    # winners peak +3000 but bank +800, and targets almost never fire
    rows = [_rt(pnl=800, reason="signal", mfe=3000, mae=-100) for _ in range(8)]
    res = si.analyze(rows)
    assert "target_too_far" in _rules(res)


def test_time_exit_leaves_profit():
    rows = ([_rt(pnl=200, reason="time_exit", mfe=2000, mae=-100)
             for _ in range(5)]
            + [_rt(pnl=500, reason="target", mfe=600) for _ in range(3)])
    res = si.analyze(rows)
    assert "time_exit_leaves_profit" in _rules(res)


def test_neg_expectancy_high_winrate():
    # 6 small wins, 2 huge losses -> wins often but bleeds
    rows = ([_rt(pnl=200, mfe=300, mae=-50) for _ in range(6)]
            + [_rt(pnl=-2000, reason="signal", mfe=100, mae=-2200)
               for _ in range(2)])
    res = si.analyze(rows)
    o = res["overall"]
    assert o["win_rate"] >= 0.55 and o["expectancy"] < 0
    assert "neg_expectancy_high_winrate" in _rules(res)


def test_iv_rank_edge_filter():
    rows = ([_rt(pnl=-400, iv=20, mfe=100, mae=-500) for _ in range(5)]
            + [_rt(pnl=900, iv=80, mfe=1200, mae=-100) for _ in range(5)])
    res = si.analyze(rows)
    assert "filter_iv_rank" in _rules(res)


def test_avoid_time_window():
    rows = ([_rt(pnl=600, tod="10:00", entry_ts="2026-07-16 10:00")
             for _ in range(5)]
            + [_rt(pnl=-400, tod="14:05", entry_ts="2026-07-16 14:05")
               for _ in range(5)])
    res = si.analyze(rows)
    assert "avoid_time_window" in _rules(res)
    assert res["by_entry_hour"]["13+"]["avg"] == -400.0


def test_fee_drag():
    rows = [_rt(pnl=300, fees=150, mfe=400, mae=-50) for _ in range(8)]
    res = si.analyze(rows)
    assert "fee_drag" in _rules(res)


def test_clean_book_suggests_nothing():
    # healthy: winners banked near their peak, one honest stop, deep-but-
    # survived drawdowns, morning entries, low fees, targets DO fire
    rows = ([_rt(pnl=900, reason="target", mfe=1000, mae=-200, fees=60)
             for _ in range(6)]
            + [_rt(pnl=-500, reason="stop_loss", mfe=100, mae=-550, fees=60)
               for _ in range(2)])
    res = si.analyze(rows)
    assert res["ready"] is True
    assert res["suggestions"] == []
