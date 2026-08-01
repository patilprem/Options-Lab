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


def test_trades_daily_endpoint_merges_counts_and_pnl(db):
    """Trade count is every fill leg — an entry row and an exit row each
    count, so a closed round trip counts as 2 (the raw activity count, not
    round trips)."""
    from app.api.strategies import trade_history_daily

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
    db.save_paper_day("SCANNER", "2026-07-22", 925.0, 0.0, 75.0, 500_925.0)
    db.save_paper_day("SCANNER", "2026-07-21", 300.0, 0.0, 40.0, 500_300.0)
    # yesterday had a booked round trip but (say) its fills predate the
    # window we query for trade-count purposes below

    data = trade_history_daily(from_date="2026-07-22", to_date="2026-07-22")

    assert len(data["days"]) == 1
    day = data["days"][0]
    assert day["date"] == "2026-07-22"
    assert day["trades"] == 2
    assert day["net_pnl"] == 925.0
    assert day["fees"] == 75.0
    assert day["gross_pnl"] == pytest.approx(1000.0)   # net + fees


def test_trades_daily_endpoint_sorted_newest_first(db):
    from app.api.strategies import trade_history_daily

    db.save_paper_day("SCANNER", "2026-07-20", 100.0, 0.0, 10.0, 0.0)
    db.save_paper_day("SCANNER", "2026-07-22", 200.0, 0.0, 10.0, 0.0)
    db.save_paper_day("SCANNER", "2026-07-21", 300.0, 0.0, 10.0, 0.0)

    data = trade_history_daily()

    dates = [d["date"] for d in data["days"]]
    assert dates == sorted(dates, reverse=True)


def test_merge_round_trips_pairs_entry_and_exit_into_one_row(db):
    """The History drill-down's whole point: an entry leg + its exit leg
    become ONE trade record, not two."""
    db.record_trade("SCANNER", "PAPER", {
        "ts": "2026-07-22 09:20:00", "contract": "NIFTY 25000 CE",
        "side": "BUY", "qty": 75, "price": 100.0, "fees": 20.0,
        "reason": "entry", "tag": "t1",
    })
    db.record_trade("SCANNER", "PAPER", {
        "ts": "2026-07-22 10:20:00", "contract": "NIFTY 25000 CE",
        "side": "SELL", "qty": 75, "price": 120.0, "fees": 22.0,
        "reason": "target", "tag": "t1",
    })

    trips = db.merge_round_trips(db.all_trades())

    assert len(trips) == 1
    t = trips[0]
    assert t["entry_ts"] == "2026-07-22 09:20:00"
    assert t["exit_ts"] == "2026-07-22 10:20:00"
    assert t["entry_price"] == 100.0 and t["exit_price"] == 120.0
    assert t["fees"] == pytest.approx(42.0)
    assert t["gross_pnl"] == pytest.approx(1500.0)
    assert t["net_pnl"] == pytest.approx(1458.0)
    assert t["reason"] == "target"


def test_merge_round_trips_uses_prestamped_pnl_when_present(db):
    """A trade booked after the net_pnl/gross_pnl fix carries its own
    stamped values on the exit leg — merge must trust those, not recompute."""
    db.record_trade("SCANNER", "PAPER", {
        "ts": "2026-07-22 09:20:00", "contract": "NIFTY 25000 PE",
        "side": "SELL", "qty": 30, "price": 200.0, "fees": 15.0,
        "reason": "entry", "tag": "t2",
    })
    db.record_trade("SCANNER", "PAPER", {
        "ts": "2026-07-22 10:20:00", "contract": "NIFTY 25000 PE",
        "side": "BUY", "qty": 30, "price": 150.0, "fees": 12.0,
        "reason": "hard_stop", "tag": "t2", "net_pnl": 1473.0, "gross_pnl": 1500.0,
    })

    trips = db.merge_round_trips(db.all_trades())

    assert trips[0]["net_pnl"] == 1473.0
    assert trips[0]["gross_pnl"] == 1500.0


def test_merge_round_trips_keeps_open_position_as_its_own_row(db):
    """A position with no exit yet must still show up (as 'open'), not
    vanish just because there's nothing to pair it with."""
    db.record_trade("SCANNER", "PAPER", {
        "ts": "2026-07-22 09:20:00", "contract": "NIFTY 25000 CE",
        "side": "BUY", "qty": 75, "price": 100.0, "fees": 20.0,
        "reason": "entry", "tag": "t3",
    })

    trips = db.merge_round_trips(db.all_trades())

    assert len(trips) == 1
    assert trips[0]["entry_ts"] == "2026-07-22 09:20:00"
    assert trips[0]["exit_ts"] is None
    assert trips[0]["exit_price"] is None
    assert trips[0]["net_pnl"] is None
    assert trips[0]["reason"] == "open"


def test_merge_round_trips_keeps_orphaned_exit_instead_of_dropping_it(db):
    """An exit with no matching entry in the fetched rows (e.g. wiped day)
    must still surface, not disappear silently."""
    db.record_trade("SCANNER", "PAPER", {
        "ts": "2026-07-22 10:20:00", "contract": "NIFTY 24000 PE",
        "side": "BUY", "qty": 75, "price": 50.0, "fees": 10.0,
        "reason": "squareoff", "tag": "t4",
    })

    trips = db.merge_round_trips(db.all_trades())

    assert len(trips) == 1
    assert trips[0]["entry_ts"] is None
    assert trips[0]["exit_ts"] == "2026-07-22 10:20:00"
    assert trips[0]["net_pnl"] is None


def test_trades_endpoint_files_an_overnight_trip_under_its_exit_day(db):
    """A position entered on day 1 and closed on day 2 must appear when
    querying day 2 (where the realized P&L belongs) and NOT when querying
    day 1 alone — merging needs the full history, not a date-bounded slice."""
    from app.api.strategies import trade_history

    db.record_trade("SCANNER", "PAPER", {
        "ts": "2026-07-21 15:00:00", "contract": "NIFTY 25000 CE",
        "side": "BUY", "qty": 75, "price": 100.0, "fees": 20.0,
        "reason": "entry", "tag": "overnight",
    })
    db.record_trade("SCANNER", "PAPER", {
        "ts": "2026-07-22 09:20:00", "contract": "NIFTY 25000 CE",
        "side": "SELL", "qty": 75, "price": 110.0, "fees": 21.0,
        "reason": "target", "tag": "overnight",
    })

    day1 = trade_history(from_date="2026-07-21", to_date="2026-07-21")
    day2 = trade_history(from_date="2026-07-22", to_date="2026-07-22")

    assert day1["count"] == 0
    assert day2["count"] == 1
    assert day2["trades"][0]["entry_ts"] == "2026-07-21 15:00:00"
    assert day2["trades"][0]["exit_ts"] == "2026-07-22 09:20:00"


def test_trades_endpoint_csv_stays_raw_per_fill(db):
    """The CSV export is the audit ledger — it must keep showing individual
    entry/exit legs, not the merged display view."""
    from app.api.strategies import trade_history

    db.record_trade("SCANNER", "PAPER", {
        "ts": "2026-07-22 09:20:00", "contract": "NIFTY 25000 CE",
        "side": "BUY", "qty": 75, "price": 100.0, "fees": 20.0,
        "reason": "entry", "tag": "t5",
    })
    db.record_trade("SCANNER", "PAPER", {
        "ts": "2026-07-22 10:20:00", "contract": "NIFTY 25000 CE",
        "side": "SELL", "qty": 75, "price": 120.0, "fees": 22.0,
        "reason": "target", "tag": "t5",
    })

    resp = trade_history(from_date="2026-07-22", to_date="2026-07-22", fmt="csv")

    lines = [l for l in resp.body.decode().splitlines() if l]
    assert len(lines) == 3   # header + 2 raw legs
