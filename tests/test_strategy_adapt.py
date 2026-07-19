"""Tests for walk-forward strategy adaptation (walkforward.adaptive_search +
strategy_adapt gates). Pure decision logic + a small end-to-end search on the
synthetic store.
"""

from __future__ import annotations

from datetime import datetime

from app.core.contract import (Action, ExpiryKind, LegSpec, OptionType,
                               StrategyMeta)
from app.data.store import SyntheticStore
from app.engines import walkforward as wf
from app.engines import strategy_adapt as SA


# --- bounded one-step candidates -------------------------------------------

def test_bounded_candidates_one_param_at_a_time():
    cands = wf._bounded_candidates({"sl_pct": 0.4, "n": 10, "on": True,
                                    "name": "x"}, rel_step=0.25)
    labels = [c[0] for c in cands]
    assert labels[0] == "current"
    # only the two numeric params move, each up and down; bool/str stay fixed
    assert set(labels) == {"current", "sl_pct_up", "sl_pct_down",
                           "n_up", "n_down"}
    by = dict(cands)
    assert by["sl_pct_up"]["sl_pct"] == 0.5 and by["sl_pct_down"]["sl_pct"] == 0.3
    assert by["n_up"]["n"] == 12 and by["n_down"]["n"] == 8   # int stays int
    # every candidate keeps the untouched params
    assert by["sl_pct_up"]["on"] is True and by["sl_pct_up"]["name"] == "x"


def test_bounded_candidates_skips_sign_flip():
    # a small positive value whose down-step would cross zero yields only 'up'
    cands = wf._bounded_candidates({"x": 0.2}, rel_step=2.0)   # step 0.4
    labels = [c[0] for c in cands]
    assert "x_down" not in labels and "x_up" in labels


# --- evaluate_search gating -------------------------------------------------

def _search(win_share, oos_m, base_m, oos_r, base_r, metric="sharpe"):
    return {"status": "ok", "metric": metric, "folds": 4,
            "recommended": {
                "label": "sl_pct_down", "param": "sl_pct",
                "params": {"sl_pct": 0.3},
                "delta": {"sl_pct": {"from": 0.4, "to": 0.3}},
                "is_win_share": win_share, "oos_metric": oos_m,
                "baseline_oos_metric": base_m, "oos_realized": oos_r,
                "baseline_oos_realized": base_r}}


def test_evaluate_requires_stability():
    # wins only 1 of 4 folds -> not stable -> no proposal
    assert SA.evaluate_search(_search(0.25, 2.0, 1.0, 5000, 3000)) is None


def test_evaluate_requires_metric_and_pnl_agreement():
    # metric better but P&L worse -> reject
    assert SA.evaluate_search(_search(0.75, 2.0, 1.0, 2000, 3000)) is None
    # P&L better but metric flat (no margin) -> reject
    assert SA.evaluate_search(_search(0.75, 1.05, 1.0, 6000, 3000)) is None


def test_evaluate_accepts_consistent_beat():
    p = SA.evaluate_search(_search(0.75, 2.0, 1.0, 6000, 3000))
    assert p and p["param"] == "sl_pct"
    assert p["delta"]["sl_pct"] == {"from": 0.4, "to": 0.3}


def test_evaluate_none_on_no_recommendation():
    assert SA.evaluate_search({"status": "ok", "recommended": None}) is None
    assert SA.evaluate_search({"status": "error"}) is None


# --- measure_forward --------------------------------------------------------

def test_measure_forward_verdicts():
    pre = [{"pnl": 500, "exit_ts": "2026-07-01 10:00"}] * 6
    post_bad = [{"pnl": -300, "exit_ts": "2026-07-20 10:00"}] * 8
    post_ok = [{"pnl": 700, "exit_ts": "2026-07-20 10:00"}] * 8
    assert SA.measure_forward(pre + post_bad, "2026-07-15")["verdict"] == "worse"
    assert SA.measure_forward(pre + post_ok, "2026-07-15")["verdict"] == "ok"
    thin = SA.measure_forward(pre + post_bad[:2], "2026-07-15")
    assert thin["ready"] is False and thin["verdict"] is None


# --- end-to-end search on the synthetic store -------------------------------

class _Tunable(wf.bt.Strategy):
    def __init__(self):
        self.params = {"sl_pct": 0.5, "entry_hhmm": 1000}

    def meta(self):
        return StrategyMeta(name="tunable", underlying="NIFTY", timeframe="5",
                            params=dict(self.params))

    def on_bar(self, ctx, bar):
        if ctx.positions:
            return
        t = ctx.now.time()
        if (t.hour * 100 + t.minute) >= self.params["entry_hhmm"]:
            ctx.enter([LegSpec(OptionType.CALL, Action.SELL, 0,
                               ExpiryKind.WEEKLY, 0, 1)], tag="x",
                      sl_pct=self.params["sl_pct"])


def test_adaptive_search_structure_and_selection_is_in_sample():
    store = SyntheticStore()
    start = datetime(2026, 7, 6, 9, 15)
    end = datetime(2026, 7, 24, 15, 30)

    def factory(p):
        s = _Tunable()
        s.params.update(p)
        return s

    res = wf.adaptive_search(factory, store, start, end,
                             {"sl_pct": 0.5, "entry_hhmm": 1000},
                             folds=3, is_frac=0.7, max_runs=200)
    assert res["status"] == "ok"
    labels = {c["label"] for c in res["candidates"]}
    assert "current" in labels and "sl_pct_up" in labels
    # baseline is the current params; every candidate reports an OOS metric
    assert res["baseline"]["label"] == "current"
    for c in res["candidates"]:
        assert "oos_metric_mean" in c and "is_wins" in c
    # runs == folds * candidates * 2 (IS + OOS)
    assert res["runs"] == 3 * len(res["candidates"]) * 2
    # total IS wins across candidates == number of folds (one winner per fold)
    assert sum(c["is_wins"] for c in res["candidates"]) == 3
