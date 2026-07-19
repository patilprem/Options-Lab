"""
Safe self-tuning for the scanner auto-trader (champion-challenger)
=================================================================
The naive adaptive loop — trade, derive a suggestion from those trades, apply
it, repeat — curve-fits the config to its own recent history and slowly kills
the system. This module is the antidote. A journal insight is only ever a
NOMINATION; a config change must then earn its way through four gates before
the human is even asked:

1. PERSISTENCE  the rule must fire on >= MIN_PERSIST_DAYS distinct reflection
   days inside a rolling window. One-day patterns are noise.
2. SHADOW TRIAL (the core defence) the changed config runs as a CHALLENGER —
   a virtual book stepped on the same live scores/chain as the real champion
   book, entering and exiting by its own rules, never touching the ledger.
   Because the config was derived from PAST trades and is evaluated only on
   FUTURE trades it never saw, this is true out-of-sample validation — the
   feedback loop is structurally broken.
3. COMPARISON   after >= MIN_TRIAL_DAYS and enough closed trades on BOTH
   books, the challenger must beat the champion's expectancy by a real margin.
   Otherwise it is discarded and the rule goes on cooldown.
4. HUMAN APPLY  a surviving challenger becomes a PROPOSAL — the "considerable
   update" prompt with the shadow-trial numbers. Nothing changes until the
   user clicks Apply. Applying takes ONE bounded step (never a jump), starts
   an EMBARGO (no new trials while the change beds in), and is measured
   afterward against the pre-change baseline so a bad apply is surfaced.

Everything here is pure decision logic; ScannerTrader wires it to the live
cycle and the registry. Strategies get the same discipline via walk-forward
(engines/walkforward.py) instead of a shadow book — same principle: derive on
one window, validate on another, never trust in-sample evidence.
"""

from __future__ import annotations

MIN_PERSIST_DAYS = 3      # rule must fire on this many distinct days...
PERSIST_WINDOW_DAYS = 10  # ...within this rolling window, to start a trial
MIN_TRIAL_DAYS = 14       # shadow trial runs at least this long (calendar)
MAX_TRIAL_DAYS = 45       # inconclusive after this -> discard
MIN_CHAL_TRADES = 8       # challenger needs this many closed trades
MIN_CHAMP_TRADES = 5      # champion needs this many in the same window
EMBARGO_DAYS = 21         # no new trials for this long after an apply
RULE_COOLDOWN_DAYS = 30   # a discarded/dismissed rule waits this long

# Which insight rules may adapt which param, by ONE bounded step per apply,
# inside hard clamps. Behavioural rules (confirmation entry, cooldowns) stay
# human-only — only scalar knobs self-tune.
ADAPTABLE: dict[str, dict] = {
    "trail_giveback":    {"param": "trail_pct",     "step": -0.05,
                          "lo": 0.10, "hi": 0.40,
                          "label": "tighten the trailing stop"},
    "tighten_hard_stop": {"param": "hard_stop_pct", "step": -0.05,
                          "lo": 0.15, "hi": 0.40,
                          "label": "tighten the hard stop"},
    "raise_entry_score": {"param": "entry_score",   "step": 5.0,
                          "lo": 50.0, "hi": 85.0,
                          "label": "raise the entry score"},
}


def _r(x, nd=2):
    return None if x is None else round(x, nd)


def _expectancy(trades: list[dict]) -> float | None:
    pnls = [t.get("realized") for t in trades if t.get("realized") is not None]
    return sum(pnls) / len(pnls) if pnls else None


def challenger_overrides(cfg_values: dict, rule: str) -> dict | None:
    """One bounded step for `rule` from the current config values, or None if
    the rule isn't adaptable / the param is already at its clamp."""
    spec = ADAPTABLE.get(rule)
    if not spec:
        return None
    cur = cfg_values.get(spec["param"])
    if cur is None:
        return None
    new = round(min(max(cur + spec["step"], spec["lo"]), spec["hi"]), 4)
    if new == round(cur, 4):
        return None                      # already at the clamp — nothing to try
    return {spec["param"]: new}


def persistent_rules(rows: list[dict],
                     min_days: int = MIN_PERSIST_DAYS) -> list[str]:
    """Rules that fired on >= min_days DISTINCT days, most-persistent first.
    rows: [{"day": "YYYY-MM-DD", "rule": str}, ...] from the insight history
    (caller restricts the window)."""
    days_by_rule: dict[str, set] = {}
    for r in rows:
        if r.get("rule") and r.get("day"):
            days_by_rule.setdefault(r["rule"], set()).add(r["day"])
    hits = [(len(d), rule) for rule, d in days_by_rule.items()
            if len(d) >= min_days]
    hits.sort(reverse=True)
    return [rule for _, rule in hits]


def compare_books(champion: list[dict], challenger: list[dict],
                  min_champ: int = MIN_CHAMP_TRADES,
                  min_chal: int = MIN_CHAL_TRADES) -> dict:
    """Champion vs challenger over the SAME forward window. `better` only when
    both books have enough closed trades AND the challenger's expectancy beats
    the champion's by >= 10% of its magnitude (any positive margin when the
    champion is flat) — a tie or a sliver is not evidence."""
    champ_exp, chal_exp = _expectancy(champion), _expectancy(challenger)
    ready = (len(challenger) >= min_chal and len(champion) >= min_champ
             and champ_exp is not None and chal_exp is not None)
    out = {"ready": ready,
           "champion": {"n": len(champion), "expectancy": _r(champ_exp),
                        "total": _r(sum(t.get("realized") or 0 for t in champion))},
           "challenger": {"n": len(challenger), "expectancy": _r(chal_exp),
                          "total": _r(sum(t.get("realized") or 0 for t in challenger))},
           "better": None}
    if ready:
        margin = chal_exp - champ_exp
        need = 0.1 * abs(champ_exp)
        out["margin"] = _r(margin)
        out["better"] = margin > need if need > 0 else margin > 0
    return out


def measure_applied(exits: list[dict], applied_ts: str,
                    min_post: int = MIN_CHAL_TRADES,
                    min_pre: int = MIN_CHAMP_TRADES) -> dict:
    """After an apply: post-change trades vs the pre-change baseline. Verdict
    'worse' (surface a revert prompt) only when the post sample is big enough
    AND both loses money and underperforms the baseline — one bad week after a
    change is not a rollback signal."""
    pre = [t for t in exits if (t.get("ts") or "") < applied_ts]
    post = [t for t in exits if (t.get("ts") or "") >= applied_ts]
    pre_exp, post_exp = _expectancy(pre), _expectancy(post)
    ready = len(post) >= min_post and len(pre) >= min_pre
    verdict = None
    if ready:
        post_total = sum(t.get("realized") or 0 for t in post)
        verdict = ("worse" if post_exp < (pre_exp or 0) and post_total < 0
                   else "ok")
    return {"ready": ready, "verdict": verdict,
            "pre": {"n": len(pre), "expectancy": _r(pre_exp)},
            "post": {"n": len(post), "expectancy": _r(post_exp)}}
