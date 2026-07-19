"""Tests for the trade-journal analytics (app/engines/journal_insights.py).

Pure: synthetic closed-trade rows in -> stats + suggestions out. Each rule is
exercised with data that should fire it, and the minimum-sample gates are
checked so a handful of trades can't produce confident advice.
"""

from __future__ import annotations

from app.engines import journal_insights as ji


def _exit(sym="AAA", realized=1000.0, reason="trail_stop", entry_score=80.0,
          entry_ts="2026-07-16T10:00:00", ts="2026-07-16T14:00:00",
          mfe=20.0, mae=20.0, ret=10.0, held_min=300,
          buildup="long_buildup", entry_fees=40.0, exit_fees=40.0):
    return {"kind": "exit", "symbol": sym, "ts": ts, "entry_ts": entry_ts,
            "realized": realized, "reason": reason, "entry_score": entry_score,
            "mfe_pct": mfe, "mae_pct": mae, "ret_pct": ret,
            "held_minutes": held_min, "entry_fees": entry_fees,
            "exit_fees": exit_fees, "entry_ctx": {"buildup": buildup}}


def _rules(res):
    return {s["rule"] for s in res["suggestions"]}


def test_insufficient_data_is_the_only_suggestion():
    res = ji.analyze([_exit(), _exit(realized=-500)])
    assert res["ready"] is False
    assert _rules(res) == {"insufficient_data"}
    assert res["overall"]["n"] == 2          # stats still computed


def test_overall_stats():
    rows = [_exit(realized=1000), _exit(realized=1000), _exit(realized=-500),
            _exit(realized=-500)] * 2
    res = ji.analyze(rows)
    ov = res["overall"]
    assert ov["n"] == 8 and ov["wins"] == 4 and ov["losses"] == 4
    assert ov["win_rate"] == 0.5
    assert ov["total"] == 2000.0
    assert ov["profit_factor"] == 2.0        # 8000 won / 4000 lost
    assert ov["total_fees"] == 8 * 80.0


def test_trail_giveback_rule_fires():
    # winners that peaked +60% but banked only +20% -> giveback 40 > half of 60
    rows = [_exit(reason="trail_stop", mfe=60.0, ret=20.0) for _ in range(8)]
    res = ji.analyze(rows)
    assert "trail_giveback" in _rules(res)


def test_trail_giveback_needs_samples():
    rows = ([_exit(reason="trail_stop", mfe=60.0, ret=20.0) for _ in range(4)]
            + [_exit(reason="target", mfe=100.0, ret=100.0) for _ in range(4)])
    res = ji.analyze(rows)                   # only 4 trail exits < MIN_BUCKET
    assert "trail_giveback" not in _rules(res)


def test_fast_hard_stops_flag_chasing():
    rows = ([_exit(realized=-800, reason="hard_stop", held_min=30,
                   mfe=2.0, mae=32.0, ret=-30.0) for _ in range(5)]
            + [_exit() for _ in range(3)])
    res = ji.analyze(rows)
    assert "fast_hard_stops" in _rules(res)


def test_raise_entry_score_when_low_band_loses():
    rows = ([_exit(entry_score=68.0, realized=-400) for _ in range(5)]
            + [_exit(entry_score=82.0, realized=900) for _ in range(5)])
    res = ji.analyze(rows)
    assert "raise_entry_score" in _rules(res)
    assert res["by_score_band"]["65-75"]["n"] == 5
    assert res["by_score_band"]["75+"]["avg"] == 900.0


def test_churn_detection_and_rule():
    # AAA stopped out then re-bought within 30 min, three times, losing money
    rows = []
    times = [("2026-07-16T10:00:00", "2026-07-16T10:40:00"),
             ("2026-07-16T10:50:00", "2026-07-16T11:30:00"),   # re-entry +10m
             ("2026-07-16T11:45:00", "2026-07-16T12:20:00"),   # re-entry +15m
             ("2026-07-16T12:40:00", "2026-07-16T13:10:00")]   # re-entry +20m
    for entry_ts, exit_ts in times:
        rows.append(_exit(sym="AAA", realized=-400, reason="hard_stop",
                          entry_ts=entry_ts, ts=exit_ts))
    rows += [_exit(sym=f"B{i}", realized=500) for i in range(4)]
    churned = ji.find_churn(rows)
    assert len(churned) == 3                 # the three quick re-entries
    res = ji.analyze(rows)
    assert "churn" in _rules(res)


def test_tighten_hard_stop_when_winners_never_draw_down():
    # every winner's worst drawdown was 5% against a 30% configured stop
    rows = [_exit(realized=800, mae=5.0) for _ in range(8)]
    res = ji.analyze(rows, config={"hard_stop_pct": 0.30})
    assert "tighten_hard_stop" in _rules(res)
    # deep-drawdown winners: the stop is earning its width -> no suggestion
    rows2 = [_exit(realized=800, mae=22.0) for _ in range(8)]
    res2 = ji.analyze(rows2, config={"hard_stop_pct": 0.30})
    assert "tighten_hard_stop" not in _rules(res2)


def test_fresh_buildup_rule():
    rows = ([_exit(buildup="long_buildup", realized=700) for _ in range(5)]
            + [_exit(buildup="short_covering", realized=-500) for _ in range(5)])
    res = ji.analyze(rows)
    assert "fresh_buildup_only" in _rules(res)


def test_late_entries_rule():
    rows = ([_exit(entry_ts="2026-07-16T10:00:00", realized=600)
             for _ in range(5)]
            + [_exit(entry_ts="2026-07-16T14:05:00", realized=-400)
               for _ in range(5)])
    res = ji.analyze(rows)
    assert "late_entries" in _rules(res)
    assert res["by_entry_hour"]["09-11"]["n"] == 5
    assert res["by_entry_hour"]["13+"]["avg"] == -400.0


def test_clean_profitable_book_suggests_nothing():
    # healthy trades: modest giveback, slow stops, deep-but-survived drawdowns,
    # fresh buildup, morning entries, low fees -> no rule should fire
    rows = ([_exit(realized=900, reason="trail_stop", mfe=50.0, ret=40.0,
                   mae=20.0) for _ in range(5)]
            + [_exit(realized=-600, reason="hard_stop", held_min=400,
                     mae=32.0, mfe=5.0, ret=-30.0) for _ in range(3)])
    res = ji.analyze(rows, config={"hard_stop_pct": 0.30})
    assert res["ready"] is True
    assert res["suggestions"] == []
