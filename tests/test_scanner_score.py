"""Offline tests for the composite setup score (F4)."""

from __future__ import annotations

from app.engines import scanner


def _t1(**kw):
    base = {"symbol": "RELIANCE", "price_change_pct": 2.0, "volume_surge": 2.0,
            "buildup": "long_buildup", "range_pos": 0.9}
    base.update(kw)
    return base


def test_strong_long_buildup_scores_high_with_reasons():
    sc = scanner.setup_score(_t1())
    assert sc["bias"] == "CE"
    assert sc["score"] >= 60
    assert any("buildup" in r for r in sc["reasons"])
    assert sc["deep_dived"] is False


def test_illiquid_chain_caps_score():
    t2 = {"liquidity": {"ok": False, "checked": 4, "bad": 3,
                        "reason": "3/4 strikes illiquid"},
          "iv_skew": None, "oi_shift": []}
    sc = scanner.setup_score(_t1(), t2)
    assert sc["score"] <= 35
    assert any("ILLIQUID" in r for r in sc["reasons"])


def test_liquid_chain_and_confirming_skew_adds():
    liquid = scanner.setup_score(
        _t1(buildup="short_buildup", price_change_pct=-2.0),
        {"liquidity": {"ok": True, "checked": 4, "bad": 0},
         "iv_skew": 5.0, "oi_shift": [{"strike_offset": -1, "option_type": "PUT",
                                       "oi_change": 300000}]})
    assert liquid["bias"] == "PE"
    assert liquid["deep_dived"] is True
    assert any("liquid" in r for r in liquid["reasons"])
    assert any("skew" in r for r in liquid["reasons"])


def test_tiny_move_scores_low():
    sc = scanner.setup_score(_t1(price_change_pct=0.05, buildup="neutral",
                                 volume_surge=None, range_pos=0.5))
    assert sc["score"] < 20
