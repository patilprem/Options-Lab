"""
History view backend: all_trades()'s scanner-name fix, daily_pnl_summary(),
and the /trades/daily per-day brief endpoint.

Offline: isolated SQLite DB per test, following the pattern in
test_portfolio_today.py.
"""

from __future__ import annotations

import pytest

from app.core import registry


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "test.db")
    registry.init_db()
    return registry


def test_all_trades_labels_scanner_rows(db):
    """The scanner books fills under strategy_id='SCANNER', which has no row
    in `strategies` — the LEFT JOIN must not leave its name blank/None."""
    db.record_trade("SCANNER", "PAPER", {
        "ts": "2026-07-22 10:30:00", "contract": "RELIANCE 1250 CALL",
        "side": "BUY", "qty": 500, "price": 20.0, "fees": 30.0,
        "margin": 0.0, "reason": "entry", "tag": "scanner:CE",
    })
    rows = db.all_trades()
    assert rows[0]["strategy"] == "Scanner Auto-Trader"
    assert rows[0]["strategy_id"] == "SCANNER"


def test_all_trades_still_names_real_strategies(db):
    """A real Strategy's name must come through unaffected by the fallback."""
    r = db.create("PBK Confluence", "x")
    db.record_trade(r.id, "PAPER", {
        "ts": "2026-07-22 10:30:00", "contract": "NIFTY 25100 CE",
        "side": "SELL", "qty": 75, "price": 100.0, "fees": 20.0,
        "margin": 5000.0, "reason": "entry", "tag": "x",
    })
    rows = db.all_trades()
    assert rows[0]["strategy"] == "PBK Confluence"


def test_daily_pnl_summary_sums_across_strategy_and_scanner(db):
    """With no strategy_id filter, a day's total is the SUM across every
    strategy AND the scanner — same underlying daily_pnl table, no separate
    join needed."""
    r = db.create("PBK Confluence", "x")
    db.save_paper_day(r.id, "2026-07-22", 500.0, 0.0, 20.0, 500_500.0)
    db.save_paper_day("SCANNER", "2026-07-22", -10_048.67, 0.0, 1200.0, 489_951.33)
    db.save_paper_day(r.id, "2026-07-21", 100.0, 0.0, 10.0, 500_100.0)   # different day

    summary = db.daily_pnl_summary(from_date="2026-07-22", to_date="2026-07-22")

    assert set(summary) == {"2026-07-22"}
    assert summary["2026-07-22"]["realized"] == pytest.approx(500.0 - 10_048.67)
    assert summary["2026-07-22"]["fees"] == pytest.approx(1220.0)


def test_daily_pnl_summary_filters_by_strategy_and_mode(db):
    r = db.create("PBK Confluence", "x")
    db.save_paper_day(r.id, "2026-07-22", 500.0, 0.0, 20.0, 500_500.0)
    db.save_paper_day("SCANNER", "2026-07-22", -1000.0, 0.0, 60.0, 499_000.0)
    db.save_day("SCANNER", "LIVE", "2026-07-22", 9999.0, 0.0, 0.0, 0.0)

    only_scanner = db.daily_pnl_summary(strategy_id="SCANNER", mode="PAPER")
    assert only_scanner["2026-07-22"]["realized"] == -1000.0

    only_strategy = db.daily_pnl_summary(strategy_id=r.id, mode="PAPER")
    assert only_strategy["2026-07-22"]["realized"] == 500.0


def test_daily_pnl_summary_empty_mode_means_all_modes(db):
    """Empty-string mode (the frontend's 'All' state) must mean unfiltered,
    the same convention all_trades() already uses — not a literal mode=''
    filter that would silently match nothing."""
    db.save_day("SCANNER", "PAPER", "2026-07-22", 100.0, 0.0, 5.0, 0.0)
    db.save_day("SCANNER", "LIVE", "2026-07-22", 50.0, 0.0, 2.0, 0.0)

    summary = db.daily_pnl_summary()

    assert summary["2026-07-22"]["realized"] == pytest.approx(150.0)
    assert summary["2026-07-22"]["fees"] == pytest.approx(7.0)


def test_trades_daily_endpoint_counts_closed_round_trips_not_fill_legs(db):
    """The trade count is CLOSED round trips (one journal 'exit' row), not
    raw fill legs — a round trip books 2 fills (entry + exit) but must count
    as 1 trade, matching the day's Net P&L (also realized/closed-only)."""
    from app.api.strategies import trade_history_daily

    # one round trip = 2 fills in the blotter, but only 1 journal exit
    db.record_trade("SCANNER", "PAPER", {
        "ts": "2026-07-22 10:30:00", "contract": "RELIANCE 1250 CALL",
        "side": "BUY", "qty": 500, "price": 20.0, "fees": 30.0,
        "margin": 0.0, "reason": "entry", "tag": "scanner:CE",
    })
    db.record_trade("SCANNER", "PAPER", {
        "ts": "2026-07-22 11:00:00", "contract": "RELIANCE 1250 CALL",
        "side": "SELL", "qty": 500, "price": 22.0, "fees": 45.0,
        "margin": 0.0, "reason": "setup_gone", "tag": "scanner:CE",
    })
    db.record_journal("RELIANCE", "entry", {"entry_price": 20.0}, ts="2026-07-22T10:30:00")
    db.record_journal("RELIANCE", "exit", {
        "entry_price": 20.0, "exit_price": 22.0, "realized": 925.0,
    }, ts="2026-07-22T11:00:00")
    db.save_paper_day("SCANNER", "2026-07-22", 925.0, 0.0, 75.0, 500_925.0)
    db.save_paper_day("SCANNER", "2026-07-21", 300.0, 0.0, 40.0, 500_300.0)
    # yesterday had a booked round trip but (say) it predates the window we
    # query for trade-count purposes below

    data = trade_history_daily(from_date="2026-07-22", to_date="2026-07-22")

    assert len(data["days"]) == 1
    day = data["days"][0]
    assert day["date"] == "2026-07-22"
    assert day["trades"] == 1                  # ONE round trip, not 2 fills
    assert day["net_pnl"] == 925.0
    assert day["fees"] == 75.0
    assert day["gross_pnl"] == pytest.approx(1000.0)   # net + fees


def test_trades_daily_endpoint_counts_strategy_round_trips_too(db):
    """Same closed-round-trip counting for a Strategy, sourced from
    strategy_journal rather than scanner_journal."""
    from app.api.strategies import trade_history_daily

    r = db.create("PBK Confluence", "x")
    db.record_strategy_journal(r.id, "exit", {
        "underlying": "NIFTY", "entry_price": 100.0, "exit_price": 80.0,
        "pnl": 1500.0,
    }, ts="2026-07-22T10:15:00")
    db.save_paper_day(r.id, "2026-07-22", 1500.0, 0.0, 45.0, 501_500.0)

    data = trade_history_daily(from_date="2026-07-22", to_date="2026-07-22")

    day = next(d for d in data["days"] if d["date"] == "2026-07-22")
    assert day["trades"] == 1
    assert day["net_pnl"] == 1500.0


def test_trades_daily_endpoint_live_mode_excludes_journal_counts(db):
    """scanner_journal/strategy_journal are paper-only — a mode=LIVE query
    must not count PAPER round trips as if they were LIVE trades."""
    from app.api.strategies import trade_history_daily

    db.record_journal("RELIANCE", "exit", {"realized": 925.0}, ts="2026-07-22T11:00:00")
    db.save_day("SCANNER", "LIVE", "2026-07-22", 500.0, 0.0, 10.0, 0.0)

    data = trade_history_daily(from_date="2026-07-22", to_date="2026-07-22", mode="LIVE")

    day = next(d for d in data["days"] if d["date"] == "2026-07-22")
    assert day["trades"] == 0
    assert day["net_pnl"] == 500.0             # the daily_pnl side still works


def test_trades_daily_endpoint_sorted_newest_first(db):
    from app.api.strategies import trade_history_daily

    db.save_paper_day("SCANNER", "2026-07-20", 100.0, 0.0, 10.0, 0.0)
    db.save_paper_day("SCANNER", "2026-07-22", 200.0, 0.0, 10.0, 0.0)
    db.save_paper_day("SCANNER", "2026-07-21", 300.0, 0.0, 10.0, 0.0)

    data = trade_history_daily()

    dates = [d["date"] for d in data["days"]]
    assert dates == sorted(dates, reverse=True)
