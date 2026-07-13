"""M6: walk-forward engine — offline, using the example strategy + SyntheticStore."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.core.loader import load_strategy_class
from app.data.store import SyntheticStore
from app.engines import walkforward as wf

CODE = Path("examples/short_straddle_920.py").read_text()


def _factory(params):
    obj = load_strategy_class(CODE)()
    if params:
        obj.params.update(params)
    return obj


# --- helpers ----------------------------------------------------------------

def test_grid_cartesian_product():
    g = wf._grid({"a": [1, 2], "b": [9]})
    assert g == [{"a": 1, "b": 9}, {"a": 2, "b": 9}]
    assert wf._grid({}) == [{}]   # no grid -> evaluate as-is


def test_windows_split_is_oos():
    w = wf._windows(datetime(2024, 1, 1, 9, 15), datetime(2024, 2, 29, 15, 30),
                    folds=2, is_frac=0.7)
    assert len(w) == 2
    is_s, is_e, oos_s, oos_e = w[0]
    assert is_s.date().isoformat() == "2024-01-01"
    assert oos_s > is_e                      # OOS strictly after IS
    assert oos_e.date() <= datetime(2024, 1, 31).date()


# --- full run ---------------------------------------------------------------

def test_run_walkforward_basic():
    calls = []
    res = wf.run_walkforward(
        _factory, SyntheticStore(), "NIFTY",
        datetime(2024, 1, 1, 9, 15), datetime(2024, 2, 29, 15, 30),
        folds=2, is_frac=0.7, param_grid={"sl_pct": [0.2, 0.4]},
        capital=600_000, metric="return_pct", max_runs=50,
        on_progress=lambda done, total, msg: calls.append((done, total)))

    assert res["status"] == "ok"
    assert len(res["folds"]) == 2
    # planned = 2 folds * (2 combos + 1 OOS) = 6
    assert res["runs"] == 6 and calls[-1] == (6, 6)

    for fold in res["folds"]:
        assert "sl_pct" in fold["best_params"]
        assert set(fold["oos"]) and fold["oos_summary"] is not None
        assert fold["oos"][0] > fold["is"][1]           # no look-ahead

    agg = res["aggregate_oos"]
    assert agg["equity_curve"] and "equity" in agg["equity_curve"][0]
    assert "return_pct" in agg and "max_drawdown_pct" in agg


def test_run_walkforward_no_grid_is_valid():
    res = wf.run_walkforward(
        _factory, SyntheticStore(), "NIFTY",
        datetime(2024, 1, 1, 9, 15), datetime(2024, 1, 31, 15, 30),
        folds=2, param_grid=None, capital=600_000, max_runs=50)
    assert res["status"] == "ok"
    assert all(f["best_params"] == {} for f in res["folds"])


def test_max_runs_guard():
    with pytest.raises(ValueError, match="exceeds max_runs"):
        wf.run_walkforward(
            _factory, SyntheticStore(), "NIFTY",
            datetime(2024, 1, 1, 9, 15), datetime(2024, 3, 31, 15, 30),
            folds=3, param_grid={"sl_pct": [0.1, 0.2, 0.3], "lots": [1, 2]},
            max_runs=5)


def test_short_range_returns_error():
    res = wf.run_walkforward(
        _factory, SyntheticStore(), "NIFTY",
        datetime(2024, 1, 1, 9, 15), datetime(2024, 1, 1, 15, 30),
        folds=4)
    assert res["status"] == "error"
