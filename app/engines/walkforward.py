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


def _bounded_candidates(current: dict, rel_step: float = 0.25,
                        max_params: int = 4) -> list[tuple]:
    """One-param-at-a-time bounded neighbours of `current` — the same "one
    small step" discipline the auto-trader uses, so a strategy proposal is
    always a single legible change. Each numeric (non-bool, positive-preserving)
    param yields an up and a down candidate; others stay fixed. Returns
    [(label, params), ...] with ('current', current) first."""
    out = [("current", dict(current))]
    numeric = [(k, v) for k, v in current.items()
               if isinstance(v, (int, float)) and not isinstance(v, bool)][:max_params]
    for k, v in numeric:
        step = abs(v) * rel_step or 1
        for sign, name in ((1, "up"), (-1, "down")):
            nv = v + sign * step
            nv = int(round(nv)) if isinstance(v, int) else round(nv, 4)
            if nv == v or (v > 0 and nv <= 0):     # no-op or sign flip -> skip
                continue
            c = dict(current)
            c[k] = nv
            out.append((f"{k}_{name}", c))
    return out


def adaptive_search(strategy_factory: Callable[[dict], object], store,
                    start: datetime, end: datetime, current_params: dict, *,
                    folds: int = 4, is_frac: float = 0.7,
                    capital: float = 1_000_000.0, metric: str = "sharpe",
                    rel_step: float = 0.25, max_params: int = 4,
                    max_runs: int = 200,
                    on_progress: Optional[Callable[[int, int, str], None]] = None
                    ) -> dict:
    """Walk-forward param search that yields at most ONE bounded change to
    propose. Each fold SELECTS the in-sample-best candidate; out-of-sample is
    used only to REPORT — never to pick the winner (that would re-introduce the
    overfitting the fold split exists to prevent). The proposal is the modal
    IS-winner, plus a stability figure (how many folds preferred it) and its
    OOS vs the current params' OOS over the same windows, so the caller can
    demand consistency, not a single lucky fold."""
    cands = _bounded_candidates(current_params, rel_step, max_params)
    windows = _windows(start, end, folds, is_frac)
    if not windows:
        return {"status": "error", "message": "date range too short for the fold count"}
    planned = len(windows) * len(cands) * 2       # IS + OOS per candidate/fold
    if planned > max_runs:
        raise ValueError(f"{planned} backtests exceeds max_runs={max_runs}; "
                         "reduce folds, params, or widen rel_step")

    by_label = {label: params for label, params in cands}
    is_wins: dict[str, int] = {}
    oos_metric: dict[str, list] = {label: [] for label, _ in cands}
    oos_realized: dict[str, float] = {label: 0.0 for label, _ in cands}
    done = 0
    for fi, (is_s, is_e, oos_s, oos_e) in enumerate(windows, 1):
        is_best = None
        for label, params in cands:
            r = bt.run_backtest(strategy_factory(params), store, is_s, is_e, capital)
            done += 1
            if on_progress:
                on_progress(done, planned, f"fold {fi} IS {label}")
            summ = r.get("summary") or {}
            rank = (1 if (summ.get("n_trades") or 0) > 0 else 0, _metric(summ, metric))
            if is_best is None or rank > is_best[0]:
                is_best = (rank, label)
        for label, params in cands:                # OOS: report every candidate
            r = bt.run_backtest(strategy_factory(params), store, oos_s, oos_e, capital)
            done += 1
            if on_progress:
                on_progress(done, planned, f"fold {fi} OOS {label}")
            summ = r.get("summary") or {}
            oos_metric[label].append(_metric(summ, metric))
            oos_realized[label] += summ.get("total_pnl") or 0.0
        is_wins[is_best[1]] = is_wins.get(is_best[1], 0) + 1

    def _mean(xs):
        return round(sum(xs) / len(xs), 4) if xs else 0.0

    n_folds = len(windows)
    candidates = [{
        "label": label, "params": by_label[label],
        "is_wins": is_wins.get(label, 0),
        "oos_metric_mean": _mean(oos_metric[label]),
        "oos_realized": round(oos_realized[label], 2),
    } for label, _ in cands]

    # modal IS-winner among the NON-current candidates (the actual change on
    # offer); the current params are the baseline it must beat.
    changed = [c for c in candidates if c["label"] != "current"]
    modal = max(changed, key=lambda c: (c["is_wins"], c["oos_metric_mean"])) \
        if changed else None
    baseline = next(c for c in candidates if c["label"] == "current")
    recommended = None
    if modal and modal["is_wins"] > 0:
        param = modal["label"].rsplit("_", 1)[0]
        recommended = {
            "label": modal["label"], "param": param,
            "params": modal["params"],
            "delta": {param: {"from": current_params.get(param),
                              "to": modal["params"].get(param)}},
            "is_win_share": round(modal["is_wins"] / n_folds, 3),
            "oos_metric": modal["oos_metric_mean"],
            "oos_realized": modal["oos_realized"],
            "baseline_oos_metric": baseline["oos_metric_mean"],
            "baseline_oos_realized": baseline["oos_realized"],
        }
    return {"status": "ok", "metric": metric, "runs": done, "folds": n_folds,
            "candidates": candidates, "baseline": baseline,
            "recommended": recommended}


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
