"""Offline tests for Tier-2 scanner analytics (F3): shortlist ranking, chain
metrics (PCR/IV/skew), liquidity screen, and OI shift. All pure — built from
constructed OptionQuote objects in the hub cache format
{(kind, offset, strike_offset, otype): OptionQuote}.

Run: venv/Scripts/python -m pytest tests/test_scanner_tier2.py -q
"""

from __future__ import annotations

from datetime import date, datetime

from app.core.contract import OptionQuote, OptionType
from app.engines import scanner


def _q(strike, otype, ltp, bid, ask, iv, oi, vol=0):
    return OptionQuote(
        ts=datetime(2026, 7, 16, 10, 0), underlying="RELIANCE",
        expiry=date(2026, 7, 31), strike=strike, option_type=otype,
        ltp=ltp, bid=bid, ask=ask, iv=iv, oi=oi, volume=vol)


def _cache(overrides=None):
    """A small ATM-centred monthly stock chain, offsets -2..+2 both sides."""
    overrides = overrides or {}
    c = {}
    for off in range(-2, 3):
        strike = 1250 + off * 10
        for otype in (OptionType.CALL, OptionType.PUT):
            key = ("MONTHLY", 0, off, otype.value)
            iv = 22 + abs(off)               # smile
            oi = 100000 - abs(off) * 10000
            c[key] = overrides.get(key, _q(strike, otype, 20.0, 19.8, 20.2, iv, oi, 5000))
    return c


# --- ranking ----------------------------------------------------------------

def test_rank_shortlist_orders_and_biases():
    metrics = {
        "AAA": {"price_change_pct": 3.0, "volume_surge": 3.0,
                "buildup": "long_buildup", "range_pos": 0.95},
        "BBB": {"price_change_pct": -2.5, "volume_surge": 2.0,
                "buildup": "short_buildup", "range_pos": 0.1},
        "CCC": {"price_change_pct": 0.1, "volume_surge": 5.0,   # too small a move
                "buildup": "long_buildup", "range_pos": 0.5},
        "DDD": {"price_change_pct": 1.0, "volume_surge": None,  # covering = weaker
                "buildup": "short_covering", "range_pos": 0.6},
    }
    sl = scanner.rank_shortlist(metrics, top_n=10, min_abs_move=0.3)
    syms = [d["symbol"] for d in sl]
    assert "CCC" not in syms                      # sub-threshold move dropped
    assert syms[0] == "AAA"                        # biggest confirmed mover first
    biases = {d["symbol"]: d["bias"] for d in sl}
    assert biases["AAA"] == "CE" and biases["BBB"] == "PE"
    assert biases["DDD"] == "CE"                   # short covering -> bullish


def test_rank_respects_top_n():
    metrics = {f"S{i}": {"price_change_pct": float(i + 1), "volume_surge": 2.0,
                         "buildup": "long_buildup", "range_pos": 0.9}
               for i in range(20)}
    assert len(scanner.rank_shortlist(metrics, top_n=15)) == 15


# --- chain metrics ----------------------------------------------------------

def test_chain_metrics_pcr_and_skew():
    # Put OI heavier than call OI -> PCR > 1; symmetric smile -> ~0 skew.
    cache = _cache()
    # bump put OI so PCR is clearly > 1
    for k, v in list(cache.items()):
        if k[3] == "PUT":
            cache[k] = _q(v.strike, OptionType.PUT, v.ltp, v.bid, v.ask,
                          v.iv, (v.oi or 0) * 2, v.volume)
    m = scanner.chain_metrics(cache)
    assert m["pcr_oi"] > 1.0
    assert m["atm_iv"] == 22                       # offset-0 IV (both sides = 22)
    assert abs(m["iv_skew"]) < 1e-9                # symmetric smile


def test_chain_metrics_downside_skew():
    # Fatten OTM put IVs -> positive skew (downside fear).
    ov = {}
    base = _cache()
    for k, v in base.items():
        if k[3] == "PUT" and k[2] < 0:
            ov[k] = _q(v.strike, OptionType.PUT, v.ltp, v.bid, v.ask,
                       (v.iv or 0) + 10, v.oi, v.volume)
    m = scanner.chain_metrics(_cache(ov))
    assert m["iv_skew"] > 5


# --- liquidity --------------------------------------------------------------

def test_liquidity_ok_when_tight():
    assert scanner.liquidity_screen(_cache())["ok"] is True


def test_liquidity_flags_wide_spread():
    ov = {}
    base = _cache()
    for k, v in base.items():
        if k[2] == 0:                              # blow out the ATM spread
            ov[k] = _q(v.strike, OptionType(k[3]), 20.0, 15.0, 25.0, v.iv, v.oi)
    res = scanner.liquidity_screen(_cache(ov), max_spread_pct=2.0)
    assert res["ok"] is False and res["bad"] >= 1


def test_liquidity_no_quotes():
    ov = {}
    base = _cache()
    for k, v in base.items():
        ov[k] = _q(v.strike, OptionType(k[3]), 20.0, None, None, v.iv, v.oi)
    assert scanner.liquidity_screen(_cache(ov))["ok"] is False


# --- OI shift ---------------------------------------------------------------

def test_oi_shift_ranks_biggest_moves():
    prev = _cache()
    cur = dict(prev)
    k = ("MONTHLY", 0, 1, "CALL")
    v = prev[k]
    cur[k] = _q(v.strike, OptionType.CALL, v.ltp, v.bid, v.ask, v.iv,
                (v.oi or 0) + 500000, v.volume)     # huge CE OI build
    shifts = scanner.oi_shift(prev, cur)
    assert shifts[0]["strike_offset"] == 1
    assert shifts[0]["option_type"] == "CALL"
    assert shifts[0]["oi_change"] == 500000
