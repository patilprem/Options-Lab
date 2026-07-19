"""
Safe self-tuning for Strategies (walk-forward champion-challenger)
=================================================================
The scanner auto-trader validates a nominated config change on a live SHADOW
book (adaptation.py). A Strategy can't do that — it's one underlying with a
backtestable history — so it uses the equivalent out-of-sample discipline:
WALK-FORWARD. The gates are the same shape, only the validator differs:

1. PERSISTENCE  a journal insight for this strategy must fire on
   >= MIN_PERSIST_DAYS distinct reflection days (adaptation.persistent_rules).
   This only ARMS a scan; it never picks the change.
2. WALK-FORWARD SEARCH  bounded one-step neighbours of the current params are
   selected IN-SAMPLE per fold and scored OUT-OF-SAMPLE (walkforward
   .adaptive_search). The proposal is the modal IS-winner — chosen on IS,
   never on the OOS window it is judged by.
3. CONSISTENCY + MARGIN  the winner must be preferred in a MAJORITY of folds
   (not one lucky split) AND beat the current params' OOS on both the metric
   and realized P&L. Otherwise: no proposal.
4. HUMAN APPLY  a survivor becomes the "considerable update" prompt with its
   walk-forward numbers. Apply sets ONE param override, starts an EMBARGO, and
   is measured forward on the paper journal against the pre-apply baseline.

Pure decision logic here; the API orchestrates the (expensive) backtests and
owns the registry state. Reuses adaptation.py's day/embargo/cooldown constants
so both loops share one discipline.
"""

from __future__ import annotations

from app.engines import adaptation as A

MIN_IS_WIN_SHARE = 0.5        # winner must lead in >= half the folds
MIN_OOS_METRIC_MARGIN = 0.15  # and beat current OOS metric by this fraction


def evaluate_search(search: dict,
                    min_win_share: float = MIN_IS_WIN_SHARE,
                    metric_margin: float = MIN_OOS_METRIC_MARGIN) -> dict | None:
    """Turn an adaptive_search() result into a proposal, or None. Demands
    stability (majority of folds) AND that the change beats the current params
    OOS on BOTH the optimization metric and realized P&L — a metric win that
    loses money, or a P&L win the metric doesn't corroborate, is not evidence."""
    if not search or search.get("status") != "ok":
        return None
    rec = search.get("recommended")
    if not rec:
        return None
    if rec["is_win_share"] < min_win_share:
        return None
    base_m = rec["baseline_oos_metric"]
    need = abs(base_m) * metric_margin
    metric_ok = (rec["oos_metric"] - base_m) > (need if need > 0 else 0)
    pnl_ok = rec["oos_realized"] > rec["baseline_oos_realized"]
    if not (metric_ok and pnl_ok):
        return None
    return {
        "param": rec["param"], "delta": rec["delta"], "params": rec["params"],
        "is_win_share": rec["is_win_share"], "metric": search.get("metric"),
        "oos_metric": rec["oos_metric"], "baseline_oos_metric": base_m,
        "oos_realized": rec["oos_realized"],
        "baseline_oos_realized": rec["baseline_oos_realized"],
        "folds": search.get("folds"),
    }


def measure_forward(journal_rows: list[dict], applied_ts: str,
                    min_post: int = A.MIN_CHAL_TRADES,
                    min_pre: int = A.MIN_CHAMP_TRADES) -> dict:
    """After a strategy apply: post-change paper trades vs the pre-change
    baseline, using the journal's realized `pnl` / `exit_ts`. Verdict 'worse'
    (surface a revert prompt) only on a big-enough post-sample that both loses
    money and underperforms the baseline."""
    def _ts(t):
        return t.get("exit_ts") or ""

    def _exp(rows):
        p = [t.get("pnl") for t in rows if t.get("pnl") is not None]
        return sum(p) / len(p) if p else None

    pre = [t for t in journal_rows if _ts(t) and _ts(t) < applied_ts]
    post = [t for t in journal_rows if _ts(t) and _ts(t) >= applied_ts]
    pre_exp, post_exp = _exp(pre), _exp(post)
    ready = len(post) >= min_post and len(pre) >= min_pre
    verdict = None
    if ready:
        post_total = sum(t.get("pnl") or 0 for t in post)
        verdict = ("worse" if post_exp < (pre_exp or 0) and post_total < 0
                   else "ok")
    return {"ready": ready, "verdict": verdict,
            "pre": {"n": len(pre), "expectancy": round(pre_exp, 2) if pre_exp is not None else None},
            "post": {"n": len(post), "expectancy": round(post_exp, 2) if post_exp is not None else None}}
