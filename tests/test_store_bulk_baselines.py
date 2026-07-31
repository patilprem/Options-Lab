"""The Tier-1 sweep's per-symbol store lookups, batched — and kept off the loop.

2026-07-31: StockScanner.sweep_once called stock_day_open_oi() and
stock_volume_baseline() once per symbol — 226 x 2 = 452 DuckDB queries per
sweep, inline on the event loop. stock_snapshots is written in timestamp order,
so `WHERE symbol=?` has no ordering to prune with and each call scanned the
whole table (~645k rows by then). py-spy caught the main thread inside
store.stock_volume_baseline with all ten worker threads idle: the process froze
for ~30s every 5 minutes, taking the chain poller's rate-gate slots and the
dashboard down with it (/token/status went 2.5ms -> >25s).

Two things are tested here, because the fix has two halves:

  * EQUIVALENCE. The batched twins must return exactly what the per-symbol
    versions return, for every symbol — including the absent/None edges. That
    equivalence is what makes the swap safe, so it's asserted symbol by symbol
    against the original methods rather than against hand-derived numbers.
  * NO REGRESSION TO THE OLD SHAPE. A structural check that the async sweep
    methods make no direct store calls at all. Batching helps only while the
    work stays off the loop; re-adding one `self.store.x()` there would quietly
    restore the freeze, and it would look perfectly reasonable in review.
"""

import ast
from datetime import date, datetime
from pathlib import Path

from app.data.store import DataStore

TODAY = date(2026, 7, 16)
PRIOR = [date(2026, 7, 13), date(2026, 7, 14), date(2026, 7, 15)]

# Volume at 09:20 and at 10:00 differ per symbol and per day, so a query that
# picks the wrong intraday row or the wrong session produces a wrong average
# rather than an accidentally-equal one.
HISTORY = {
    "RELIANCE": [(1_000_000, 1_400_000), (2_000_000, 2_600_000), (3_000_000, 3_300_000)],
    "TCS":      [(500_000, 900_000),     (700_000, 1_100_000),    (600_000, 1_050_000)],
    "INFY":     [(250_000, 480_000),     (310_000, 505_000),      (290_000, 512_000)],
    # WIPRO has prior history but NO rows today -> day-open lookup finds nothing
    "WIPRO":    [(120_000, 210_000),     (140_000, 230_000),      (130_000, 225_000)],
}
# HDFCBANK trades today for the first time: no prior sessions at all, so the
# baseline is None on both paths.
TODAY_ONLY = ["HDFCBANK"]
NO_ROWS_TODAY = ["WIPRO"]

REF_TOD = 10 * 3600            # 10:00, matching the later snapshot each day


def _snap(sym, ts, volume, oi):
    return {"symbol": sym, "ts": ts, "spot": None, "fut_ltp": 1200.0,
            "day_open": 1200, "day_high": 1210, "day_low": 1195,
            "prev_close": 1198, "volume": volume, "oi": oi}


def _store(tmp_path) -> DataStore:
    st = DataStore(tmp_path / "m.duckdb")
    rows = []
    for sym, days in HISTORY.items():
        for d, (v0920, v1000) in zip(PRIOR, days):
            rows.append(_snap(sym, datetime(d.year, d.month, d.day, 9, 20), v0920, 900_000))
            rows.append(_snap(sym, datetime(d.year, d.month, d.day, 10, 0), v1000, 950_000))
    # today: two snapshots, OI moving, so first() vs last() is distinguishable
    for sym in list(HISTORY) + TODAY_ONLY:
        if sym in NO_ROWS_TODAY:
            continue
        rows.append(_snap(sym, datetime(2026, 7, 16, 9, 20), 100_000, 1_000_000))
        rows.append(_snap(sym, datetime(2026, 7, 16, 10, 0), 400_000, 1_250_000))
    st.upsert_stock_snapshots(rows)
    return st


ALL_SYMBOLS = list(HISTORY) + TODAY_ONLY


# --- equivalence with the per-symbol methods ---------------------------------

def test_day_open_oi_bulk_matches_per_symbol(tmp_path):
    st = _store(tmp_path)
    bulk = st.stock_day_open_oi_bulk(TODAY)
    for sym in ALL_SYMBOLS:
        assert bulk.get(sym, (None, None)) == st.stock_day_open_oi(sym, TODAY), sym


def test_day_open_oi_bulk_picks_first_row_of_the_day(tmp_path):
    """Guards the equivalence test against being vacuously true — if both
    methods returned the 10:00 row this would fail."""
    st = _store(tmp_path)
    assert st.stock_day_open_oi_bulk(TODAY)["RELIANCE"][0] == 1_000_000


def test_volume_baseline_bulk_matches_per_symbol(tmp_path):
    st = _store(tmp_path)
    bulk = st.stock_volume_baseline_bulk(REF_TOD, TODAY)
    for sym in ALL_SYMBOLS:
        assert bulk.get(sym) == st.stock_volume_baseline(sym, REF_TOD, TODAY), sym


def test_volume_baseline_bulk_averages_the_10am_rows(tmp_path):
    """Again non-vacuous: the 09:20 volumes would give a different mean."""
    st = _store(tmp_path)
    expected = (1_400_000 + 2_600_000 + 3_300_000) / 3
    assert st.stock_volume_baseline_bulk(REF_TOD, TODAY)["RELIANCE"] == expected


def test_bulk_honours_the_ref_time_when_it_picks_the_earlier_row(tmp_path):
    """Same data, ref time at 09:20 — both paths must switch to the earlier
    snapshot of each session, together."""
    st = _store(tmp_path)
    early = 9 * 3600 + 20 * 60
    bulk = st.stock_volume_baseline_bulk(early, TODAY)
    assert bulk["RELIANCE"] == (1_000_000 + 2_000_000 + 3_000_000) / 3
    for sym in ALL_SYMBOLS:
        assert bulk.get(sym) == st.stock_volume_baseline(sym, early, TODAY), sym


def test_bulk_honours_the_days_window(tmp_path):
    """days=2 must drop the oldest session on both paths."""
    st = _store(tmp_path)
    bulk = st.stock_volume_baseline_bulk(REF_TOD, TODAY, days=2)
    assert bulk["RELIANCE"] == (2_600_000 + 3_300_000) / 2
    for sym in ALL_SYMBOLS:
        assert bulk.get(sym) == st.stock_volume_baseline(
            sym, REF_TOD, TODAY, days=2), sym


# --- the absent/None edges the caller relies on ------------------------------

def test_symbol_with_no_prior_history_is_absent_not_zero(tmp_path):
    """A first-day symbol must read as 'no baseline' (None), never 0 — a 0
    baseline would make the surge ratio divide by zero or read as infinite."""
    st = _store(tmp_path)
    bulk = st.stock_volume_baseline_bulk(REF_TOD, TODAY)
    assert "HDFCBANK" not in bulk
    assert bulk.get("HDFCBANK") is None
    assert st.stock_volume_baseline("HDFCBANK", REF_TOD, TODAY) is None


def test_symbol_with_no_rows_today_is_absent_from_day_open(tmp_path):
    st = _store(tmp_path)
    bulk = st.stock_day_open_oi_bulk(TODAY)
    assert "WIPRO" not in bulk
    assert bulk.get("WIPRO", (None, None)) == (None, None)
    assert st.stock_day_open_oi("WIPRO", TODAY) == (None, None)


def test_empty_day_returns_empty_dicts_not_an_error(tmp_path):
    st = _store(tmp_path)
    assert st.stock_day_open_oi_bulk(date(2026, 1, 1)) == {}
    assert st.stock_volume_baseline_bulk(REF_TOD, date(2026, 1, 1)) == {}


# --- structural: the sweep must not touch the store on the event loop --------

SCANNER = Path(__file__).resolve().parents[1] / "app" / "engines" / "scanner.py"


def _async_fn(name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(SCANNER.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"async def {name} not found in scanner.py")


def _direct_store_calls(fn) -> list:
    """Names of `self.store.X(...)` calls made directly in this function body.

    Passing `self.store` as an ARGUMENT (the run_in_executor form) is not a
    call and is correctly not reported."""
    found = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Attribute)
                and f.value.attr == "store"
                and isinstance(f.value.value, ast.Name)
                and f.value.value.id == "self"):
            found.append(f.attr)
    return found


def test_sweep_once_makes_no_direct_store_calls():
    calls = _direct_store_calls(_async_fn("sweep_once"))
    assert calls == [], (
        f"sweep_once calls self.store.{calls} on the event loop — this is the "
        f"exact shape that froze the process for ~30s every 5 minutes. Move it "
        f"into _persist_and_load_baselines (or another run_in_executor hop).")


def test_tier2_once_makes_no_direct_store_calls():
    calls = _direct_store_calls(_async_fn("tier2_once"))
    assert calls == [], (
        f"tier2_once calls self.store.{calls} on the event loop; run it via "
        f"run_in_executor instead.")
