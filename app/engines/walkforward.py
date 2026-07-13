"""
Walk-Forward Analysis (M6)
==========================
Guards against curve-fitting: instead of one backtest over the whole range,
split it into K folds; on each fold optimize params on an IN-SAMPLE window,
then measure those params on the untouched OUT-OF-SAMPLE window that follows.
The aggregate OOS equity curve is the honest, un-fitted performance estimate.

Pure engine: it takes a `strategy_factory(params) -> Strategy` and a store, and
reuses backtest.run_backtest for every evaluation. The API layer builds the
factory from a strategy's code and wires `on_progress` to the events log.
"""

from __future__ import annotations

import itertools
from datetime import datetime, time, timedelta
from typing import Callable, Optional

from app.engines import backtest as bt

_DAY_OPEN = time(9, 15)
_DAY_CLOSE = time(15, 30)

# Metrics where a HIGHER value is better (all current summary metrics are).
_BETTER_HIGH = {"sharpe", "return_pct", "total_pnl", "win_rate_pct"}


def _grid(param_grid: dict[str, list]) -> list[dict]:
    """Cartesian product of {param: [values]} -> list of param dicts.
    Empty grid -> a single empty override (evaluate the strategy as-is)."""
    if not param_grid:
        return [{}]
    keys = list(param_grid)
    return [dict(zip(keys, combo))
            for combo in itertools.product(*(param_grid[k] for k in keys))]


def _windows(start: datetime, end: datetime, folds: int, is_frac: float):
    """Split [start, end] into `folds` contiguous windows; each yields an
    in-sample [is_start, is_end] and out-of-sample [oos_start, oos_end]."""
    d0, d1 = start.date(), end.date()
    total_days = (d1 - d0).days + 1
    span = total_days // folds
    out = []
    for i in range(folds):
        w_start = d0 + timedelta(days=i * span)
        w_end = d1 if i == folds - 1 else d0 + timedelta(days=(i + 1) * span - 1)
        is_days = max(1, int((w_end - w_start).days * is_frac))
        is_end = w_start + timedelta(days=is_days)
        oos_start = is_end + timedelta(days=1)
        if oos_start > w_end:               # degenerate fold, skip
            continue
        out.append((
            datetime.combine(w_start, _DAY_OPEN), datetime.combine(is_end, _DAY_CLOSE),
            datetime.combine(oos_start, _DAY_OPEN), datetime.combine(w_end, _DAY_CLOSE)))
    return out


def _metric(summary: dict, metric: str) -> float:
    v = summary.get(metric, 0.0)
    return v if v is not None else 0.0


def run_walkforward(strategy_factory: Callable[[dict], object], store, underlying: str,
                    start: datetime, end: datetime, *, folds: int = 4,
                    is_frac: float = 0.7, param_grid: Optional[dict] = None,
                    capital: float = 1_000_000.0, metric: str = "sharpe",
                    max_runs: int = 200,
                    on_progress: Optional[Callable[[int, int, str], None]] = None) -> dict:
    """Run K-fold walk-forward. Returns per-fold OOS results + the chained
    aggregate OOS equity curve. Raises ValueError if the run count would exceed
    `max_runs` (reduce the grid or folds)."""
    combos = _grid(param_grid or {})
    windows = _windows(start, end, folds, is_frac)
    if not windows:
        return {"status": "error", "message": "date range too short for the fold count"}

    planned = len(windows) * (len(combos) + 1)  # IS grid + 1 OOS eval per fold
    if planned > max_runs:
        raise ValueError(f"{planned} backtests exceeds max_runs={max_runs}; "
                         "reduce folds or param ranges")

    done = 0
    fold_results = []
    for fi, (is_s, is_e, oos_s, oos_e) in enumerate(windows, 1):
        # --- in-sample grid search ---
        best = None
        for params in combos:
            res = bt.run_backtest(strategy_factory(params), store, is_s, is_e, capital)
            done += 1
            if on_progress:
                on_progress(done, planned, f"fold {fi}/{len(windows)} IS {params}")
            summ = res.get("summary") or {}
            score = _metric(summ, metric)
            # prefer param sets that actually traded in-sample
            traded = (summ.get("n_trades") or 0) > 0
            rank = (1 if traded else 0, score)
            if best is None or rank > best[0]:
                best = (rank, params, summ)

        best_params = best[1] if best else {}
        # --- out-of-sample evaluation with the winning params ---
        oos = bt.run_backtest(strategy_factory(best_params), store, oos_s, oos_e, capital)
        done += 1
        if on_progress:
            on_progress(done, planned, f"fold {fi}/{len(windows)} OOS {best_params}")
        oos_summ = oos.get("summary") or {}
        oos_daily = oos.get("daily") or []
        fold_results.append({
            "fold": fi,
            "is": [is_s.date().isoformat(), is_e.date().isoformat()],
            "oos": [oos_s.date().isoformat(), oos_e.date().isoformat()],
            "best_params": best_params,
            "is_metric": round(_metric(best[2], metric), 4) if best else 0.0,
            "oos_summary": oos_summ,
            "oos_daily": [{"date": d["date"], "realized": d.get("realized", 0.0),
                           "equity_eod": d.get("equity_eod", capital)} for d in oos_daily],
        })

    aggregate = _aggregate_oos(fold_results, capital, metric)
    return {"status": "ok", "metric": metric, "runs": done,
            "folds": fold_results, "aggregate_oos": aggregate}


def _aggregate_oos(fold_results: list[dict], capital: float, metric: str) -> dict:
    """Chain each fold's OOS realized P&L into one continuous equity curve."""
    curve = []
    equity = capital
    total_realized = 0.0
    for fr in fold_results:
        for d in fr["oos_daily"]:
            equity += d["realized"]
            total_realized += d["realized"]
            curve.append({"date": d["date"], "equity": round(equity, 2)})
    peak = capital
    max_dd = 0.0
    for pt in curve:
        peak = max(peak, pt["equity"])
        max_dd = max(max_dd, (peak - pt["equity"]) / peak if peak else 0.0)
    return {
        "equity_curve": curve,
        "total_realized": round(total_realized, 2),
        "return_pct": round(total_realized / capital * 100, 2) if capital else 0.0,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "days": len(curve),
    }
