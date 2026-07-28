"""The deep-dive shortlist and the entry gate must rank on the same formula.

2026-07-28: three names cleared the trader's entry bar (TORNTPHARM 70.6,
CONCOR 70.4, TECHM 67.4) and NONE of them were in the Tier-2 shortlist, so
none had chain data. pick_entries selected all three, _atm_quote returned
None for each, and the entry loop dropped them with a bare `continue`. From
outside it was indistinguishable from "no setup qualified" — the scanner
looked healthy, 15 names deep-dived, zero trades.

Cause: rank_shortlist() ranked on its own move x buildup x surge product
while pick_entries gates on setup_score >= entry_score. Two scales, nothing
keeping them aligned. It had traded 11 times the previous day only because
the two sets happened to overlap.
"""

from app.engines.scanner import (SHORTLIST_SIZE, rank_for_deep_dive,
                                 setup_score)
from app.engines.scanner_trader import TradeConfig, pick_entries


def _m(symbol, pc, surge=1.0, buildup="long_buildup", range_pos=0.9):
    return {"symbol": symbol, "price_change_pct": pc, "volume_surge": surge,
            "buildup": buildup, "range_pos": range_pos}


def test_shortlist_is_ranked_by_the_entry_formula():
    """THE regression. Whatever clears entry_score must be in the shortlist."""
    metrics = {f"SYM{i}": _m(f"SYM{i}", 0.5 + i * 0.15, surge=1.0 + i * 0.1)
               for i in range(30)}
    shortlist = {d["symbol"] for d in rank_for_deep_dive(metrics, top_n=SHORTLIST_SIZE)}

    cfg = TradeConfig()
    ranked = sorted((setup_score(m, None) for m in metrics.values()),
                    key=lambda s: s.get("score") or 0, reverse=True)
    qualifying = [s["symbol"] for s in ranked
                  if (s.get("score") or 0) >= cfg.entry_score and s.get("bias")]

    assert qualifying, "test needs at least one qualifying name"
    missing = [s for s in qualifying[:SHORTLIST_SIZE] if s not in shortlist]
    assert not missing, (
        f"{missing} clear entry_score but would get no chain data — the "
        f"trader will pick them and silently fail to price them")


def test_every_name_pick_entries_returns_is_in_the_shortlist():
    """End to end: what the trader selects must be priceable."""
    metrics = {f"S{i}": _m(f"S{i}", 0.4 + i * 0.2, surge=1.0 + i * 0.15)
               for i in range(25)}
    shortlist = {d["symbol"] for d in rank_for_deep_dive(metrics)}
    ranked = sorted((setup_score(m, None) for m in metrics.values()),
                    key=lambda s: s.get("score") or 0, reverse=True)
    picked = pick_entries(ranked, set(), TradeConfig())
    assert picked, "test needs at least one entry"
    assert all(p in shortlist for p in picked), (
        f"picked {picked} but shortlist is {sorted(shortlist)}")


def test_unbiased_names_never_waste_a_deep_dive():
    """15 chain fetches through a 3s rate gate; an unbiased setup can never be
    entered, so spending one on it is pure waste."""
    metrics = {"FLAT": _m("FLAT", 0.0, buildup=None),
               "MOVER": _m("MOVER", 2.0)}
    syms = [d["symbol"] for d in rank_for_deep_dive(metrics)]
    assert "MOVER" in syms
    assert all(setup_score(metrics[s], None).get("bias") for s in syms)


def test_shortlist_respects_top_n():
    metrics = {f"S{i}": _m(f"S{i}", 1.0 + i * 0.1) for i in range(40)}
    assert len(rank_for_deep_dive(metrics, top_n=15)) == 15
    assert len(rank_for_deep_dive(metrics, top_n=3)) == 3


def test_shortlist_is_ordered_best_first():
    metrics = {"LOW": _m("LOW", 0.4, surge=1.0),
               "HIGH": _m("HIGH", 3.0, surge=3.0)}
    assert [d["symbol"] for d in rank_for_deep_dive(metrics)][0] == "HIGH"


def test_shortlist_keeps_the_expected_shape():
    """ScannerTrader and the UI read these keys off shortlist entries."""
    d = rank_for_deep_dive({"X": _m("X", 2.0)})[0]
    for key in ("symbol", "score", "bias", "buildup", "price_change_pct",
                "volume_surge", "range_pos"):
        assert key in d, f"shortlist entry lost the {key!r} key"


def test_empty_metrics_is_empty_shortlist():
    assert rank_for_deep_dive({}) == []


def test_deep_dive_can_only_raise_a_score():
    """Why we take top-N rather than filtering at entry_score: chain quality,
    skew and OI shift ADD up to 15, so a name below the bar on Tier-1 alone
    can clear it once deep-dived. Filtering early would exclude exactly those."""
    m = _m("X", 1.2)
    t1_only = setup_score(m, None)["score"]
    with_chain = setup_score(m, {"liquidity": {"ok": True, "checked": True},
                                 "iv_skew": None, "oi_shift": []})["score"]
    assert with_chain > t1_only
