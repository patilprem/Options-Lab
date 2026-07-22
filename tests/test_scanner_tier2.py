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


# --- tier2_once "did nothing" diagnostics -----------------------------------

class _NullStore:
    pass


def _events(monkeypatch):
    """Capture registry.record_event(level, cat, msg) tuples."""
    import asyncio

    from app.core import registry
    seen = []
    monkeypatch.setattr(registry, "record_event",
                        lambda lvl, cat, msg: seen.append((lvl, cat, msg)))
    return seen, asyncio


def test_tier2_logs_empty_shortlist(monkeypatch):
    """A quiet board (no stock clears the min move) must SAY it skipped, not go
    silent — otherwise an all-Tier-1 scanner looks broken when it's just calm."""
    seen, asyncio = _events(monkeypatch)
    sc = scanner.StockScanner(_NullStore())
    sc.metrics = {"AAA": {"price_change_pct": 0.05, "volume_surge": 1.0,
                          "buildup": "neutral"}}          # sub-0.3% -> no shortlist
    done = asyncio.run(sc.tier2_once(hub=None, loop=None))
    assert done == 0
    assert any("shortlist empty" in m for _l, _c, m in seen)


def test_tier2_logs_shortlist_without_chain_cfg(monkeypatch):
    """A real mover that can't resolve a cash-equity id is a gap worth a warn,
    distinct from a quiet market."""
    seen, asyncio = _events(monkeypatch)
    sc = scanner.StockScanner(_NullStore())
    sc.metrics = {"AAA": {"price_change_pct": 2.0, "volume_surge": 3.0,
                          "buildup": "long_buildup", "range_pos": 0.9}}
    sc._universe = {"AAA": {"future_security_id": 1, "spot_security_id": None}}
    done = asyncio.run(sc.tier2_once(hub=None, loop=None))
    assert done == 0
    assert any(lvl == "warn" and "cash-equity id" in m for lvl, _c, m in seen)


# --- position_mtm_once: the fast, held-positions-only mark/exit loop --------
# (the guard/skip paths, matching the tier2_once diagnostics above — the
# "successfully polled a held symbol" happy path is thin async glue wired the
# same way tier2_once's own happy path is, and isn't separately unit tested
# either, to avoid mutating the shared dhan_client.UNDERLYINGS registry.)

def _iso_registry(tmp_path, monkeypatch):
    from app.core import registry
    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "test.db")
    registry.init_db()
    return registry


def test_position_mtm_noop_without_a_trader(tmp_path, monkeypatch):
    import asyncio
    _iso_registry(tmp_path, monkeypatch)
    sc = scanner.StockScanner(_NullStore())
    sc.trader = None
    assert asyncio.run(sc.position_mtm_once(hub=None, loop=None)) == 0


def test_position_mtm_noop_when_scanner_trade_off(tmp_path, monkeypatch):
    import asyncio

    class _Trader:
        def held_symbols(self):
            return ["AAA"]        # would be non-empty if the guard didn't fire

    reg = _iso_registry(tmp_path, monkeypatch)
    reg.set_setting("scanner_trade", "off")
    sc = scanner.StockScanner(_NullStore())
    sc.trader = _Trader()
    assert asyncio.run(sc.position_mtm_once(hub=None, loop=None)) == 0


def test_position_mtm_noop_when_nothing_open(tmp_path, monkeypatch):
    import asyncio

    class _Trader:
        def held_symbols(self):
            return []

    reg = _iso_registry(tmp_path, monkeypatch)
    reg.set_setting("scanner_trade", "on")
    sc = scanner.StockScanner(_NullStore())
    sc.trader = _Trader()
    assert asyncio.run(sc.position_mtm_once(hub=None, loop=None)) == 0


def test_position_mtm_skips_oversized_book_with_a_warning(tmp_path, monkeypatch):
    """A book bigger than MAX_FAST_MTM_SYMBOLS must not queue that many
    requests onto the shared rate gate every ~20s — it skips (falling back to
    the next Tier-2 cycle) and says why, rather than silently over-polling."""
    class _Trader:
        def held_symbols(self):
            return [f"S{i}" for i in range(scanner.MAX_FAST_MTM_SYMBOLS + 1)]

    reg = _iso_registry(tmp_path, monkeypatch)
    reg.set_setting("scanner_trade", "on")
    seen, asyncio = _events(monkeypatch)
    sc = scanner.StockScanner(_NullStore())
    sc.trader = _Trader()
    assert asyncio.run(sc.position_mtm_once(hub=None, loop=None)) == 0
    assert any(lvl == "warn" and "exceeds" in m for lvl, _c, m in seen)


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
