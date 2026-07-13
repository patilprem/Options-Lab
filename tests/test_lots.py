"""Dated lot-size table — effective dates from NSE/BSE circulars.

Backtests replay 2024-2026 where NIFTY lots went 25 -> 75 -> 65; a flat
constant mis-sizes every fill on the far side of a revision.
"""

from datetime import date

from app.engines.backtest import lot_size_on


def test_nifty_lot_history():
    assert lot_size_on("NIFTY", date(2024, 10, 1)) == 25    # pre SEBI hike
    assert lot_size_on("NIFTY", date(2024, 11, 20)) == 75   # hike effective day
    assert lot_size_on("NIFTY", date(2025, 6, 15)) == 75
    assert lot_size_on("NIFTY", date(2025, 12, 30)) == 75   # last old-series day
    assert lot_size_on("NIFTY", date(2026, 1, 6)) == 65     # Jan-2026 series
    assert lot_size_on("NIFTY", date(2026, 7, 14)) == 65


def test_banknifty_went_up_then_back_down():
    assert lot_size_on("BANKNIFTY", date(2024, 10, 1)) == 15
    assert lot_size_on("BANKNIFTY", date(2025, 1, 15)) == 30
    assert lot_size_on("BANKNIFTY", date(2025, 5, 15)) == 35   # FAOP64625
    assert lot_size_on("BANKNIFTY", date(2026, 2, 1)) == 30    # FAOP70616


def test_default_today_and_unknown_underlying():
    assert lot_size_on("NIFTY") == lot_size_on("NIFTY", date.today())
    assert lot_size_on("CRUDEOIL", date(2026, 1, 1)) == 50     # MCX fallback


def test_contexts_expose_current_lot(tmp_path, monkeypatch):
    """Paper context lot_size must track the dated table, not a constant."""
    from app.core import registry
    from app.data.store import SyntheticStore
    from app.engines.paper import MarketHub, PaperContext

    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "t.db")
    registry.init_db()
    rec = registry.create("x", "y")
    ctx = PaperContext(rec, "NIFTY", MarketHub(SyntheticStore()), "5")
    assert ctx.lot_size == lot_size_on("NIFTY")
