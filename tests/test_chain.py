"""Offline tests for the M3 option-chain normalizer, expiry resolution, and the
MarketHub live-quote cache. Replays a REAL trimmed chain fixture; no network."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from app.core.contract import Action, ExpiryKind, LegSpec, OptionQuote, OptionType
from app.engines import chain as chainmod
from app.engines.paper import MarketHub

FIX = Path(__file__).parent / "fixtures"
TS = datetime(2026, 7, 9, 10, 30)


def _chain():
    return json.loads((FIX / "option_chain_nifty.json").read_text())


# --- normalize_chain --------------------------------------------------------

def test_normalize_keys_and_offsets():
    q = chainmod.normalize_chain(_chain(), "NIFTY", "WEEKLY", 0,
                                 date(2026, 7, 14), TS)
    # 5 strikes (ATM +/-2) x 2 sides
    assert len(q) == 10
    for off in (-2, -1, 0, 1, 2):
        assert ("WEEKLY", 0, off, "CALL") in q
        assert ("WEEKLY", 0, off, "PUT") in q


def test_normalize_atm_call_values():
    q = chainmod.normalize_chain(_chain(), "NIFTY", "WEEKLY", 0,
                                 date(2026, 7, 14), TS)
    ce = q[("WEEKLY", 0, 0, "CALL")]
    assert isinstance(ce, OptionQuote)
    assert ce.strike == 23950.0 and ce.option_type == OptionType.CALL
    assert ce.expiry == date(2026, 7, 14) and ce.ts == TS
    assert ce.ltp == 145.0 and ce.bid == 145.4 and ce.ask == 146.0
    assert round(ce.iv, 4) == 10.5731
    assert ce.delta == 0.56408 and ce.gamma == 0.00131
    # PE side is distinct
    assert q[("WEEKLY", 0, 0, "PUT")].ltp == 124.2


def test_normalize_handles_predescended_payload():
    inner = _chain()["data"]  # {last_price, oc} — already descended
    q = chainmod.normalize_chain(inner, "NIFTY", "WEEKLY", 0, date(2026, 7, 14), TS)
    assert q[("WEEKLY", 0, 0, "CALL")].strike == 23950.0


def test_normalize_empty_when_no_spot():
    assert chainmod.normalize_chain({"oc": {}}, "NIFTY", "WEEKLY", 0,
                                    date(2026, 7, 14), TS) == {}


# --- resolve_expiry ---------------------------------------------------------

EXPIRIES = ["2026-07-14", "2026-07-21", "2026-07-28",
            "2026-08-04", "2026-08-11", "2026-08-25", "2026-09-29"]


def test_resolve_weekly():
    assert chainmod.resolve_expiry(EXPIRIES, "WEEKLY", 0) == "2026-07-14"
    assert chainmod.resolve_expiry(EXPIRIES, "WEEKLY", 2) == "2026-07-28"
    assert chainmod.resolve_expiry(EXPIRIES, "WEEKLY", 99) is None


def test_resolve_monthly_picks_month_end():
    # last expiry within each calendar month
    assert chainmod.resolve_expiry(EXPIRIES, "MONTHLY", 0) == "2026-07-28"
    assert chainmod.resolve_expiry(EXPIRIES, "MONTHLY", 1) == "2026-08-25"
    assert chainmod.resolve_expiry(EXPIRIES, "MONTHLY", 2) == "2026-09-29"


# --- MarketHub live-quote cache ---------------------------------------------

class _Store:
    def __init__(self): self.calls = 0
    def option_close(self, *a, **k):
        self.calls += 1
        return "STORE_FALLBACK"


def test_quote_uses_chain_cache_then_falls_back():
    store = _Store()
    hub = MarketHub(store)
    hub._chain_cache["NIFTY"] = chainmod.normalize_chain(
        _chain(), "NIFTY", "WEEKLY", 0, date(2026, 7, 14), TS)

    leg = LegSpec(OptionType.CALL, Action.SELL, strike_offset=0,
                  expiry_kind=ExpiryKind.WEEKLY, lots=1)
    fresh_ts = TS + timedelta(minutes=5)         # within QUOTE_MAX_AGE_S
    q = hub.quote("NIFTY", fresh_ts, leg)
    assert isinstance(q, OptionQuote) and q.strike == 23950.0
    assert q.ts == fresh_ts               # ts refreshed to the query time
    assert store.calls == 0               # served from cache, no store hit

    # STALENESS GUARD: a cache that stopped updating must refuse to price new
    # entries (frozen-chain fills, 2026-07-13) — None, not a stale quote, and
    # NOT the (even staler) store.
    stale_ts = TS + timedelta(hours=1)
    assert hub.quote("NIFTY", stale_ts, leg) is None
    assert store.calls == 0

    # a strike we didn't cache -> store fallback
    leg_far = LegSpec(OptionType.CALL, Action.SELL, strike_offset=10,
                      expiry_kind=ExpiryKind.WEEKLY, lots=1)
    assert hub.quote("NIFTY", TS, leg_far) == "STORE_FALLBACK"
    assert store.calls == 1
