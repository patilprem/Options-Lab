"""Feed self-heal: the watchdog must not merely alert — during market hours a
down or silent feed (a dead/rolled MCX contract, a stale socket LiveFeed can't
tell is dead) must trigger an automatic re-resolve + resubscribe so recording
recovers without a human.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.data import dhan_client as dc
from app.data.store import SyntheticStore
from app.engines.paper import MarketHub
from app.engines.watchdog import feed_broken


# -- pure predicate ---------------------------------------------------------

def test_feed_broken_only_during_market_hours_and_live():
    live_ok = {"mode": "live", "connected": True, "tick_age_sec": 10}
    assert feed_broken(live_ok, market_open=True) is False
    assert feed_broken(live_ok, market_open=False) is False       # off-hours
    # synthetic / off never counts as broken
    assert feed_broken({"mode": "synthetic", "connected": True}, True) is False


def test_feed_broken_flags_down_and_silent():
    assert feed_broken({"mode": "live", "connected": False}, True) is True
    assert feed_broken({"mode": "live", "connected": True,
                        "tick_age_sec": None}, True) is True       # never ticked
    assert feed_broken({"mode": "live", "connected": True,
                        "tick_age_sec": 999}, True) is True        # silent >180s


# -- recovery action --------------------------------------------------------

class _Feed:
    def __init__(self):
        self.refreshes = 0

    def refresh(self):
        self.refreshes += 1


def _hub_with_feed():
    hub = MarketHub(SyntheticStore())
    hub._started = True
    hub._livefeed = _Feed()
    return hub


def test_self_heal_reresolves_mcx_and_resubscribes(monkeypatch):
    called = {"n": 0}

    def fake_resolve(*a, **k):
        called["n"] += 1
        return {"CRUDEOIL": 111, "GOLD": 222}

    monkeypatch.setattr(dc, "resolve_mcx_ids", fake_resolve)
    hub = _hub_with_feed()
    hub._chain_only.add("CRUDEOIL")                         # MCX name is tracked
    hub._expiries_cache = {"CRUDEOIL": (date(2026, 7, 16), ["x"]),
                           "GOLD": (date(2026, 7, 16), ["y"]),
                           "NIFTY": (date(2026, 7, 16), ["z"])}

    out = hub.self_heal_feed()

    assert called["n"] == 1 and out["resolved"] == {"CRUDEOIL": 111, "GOLD": 222}
    assert out["resubscribed"] is True and hub._livefeed.refreshes == 1
    # stale MCX expiries dropped (new contract → new expiries); NSE untouched
    assert "CRUDEOIL" not in hub._expiries_cache
    assert "GOLD" not in hub._expiries_cache
    assert "NIFTY" in hub._expiries_cache


def test_self_heal_skips_mcx_resolve_when_no_mcx_tracked(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not resolve MCX when none is tracked")

    monkeypatch.setattr(dc, "resolve_mcx_ids", boom)
    hub = _hub_with_feed()
    hub._wanted["NIFTY"] = {5}                              # NSE only, no MCX

    out = hub.self_heal_feed()

    assert out["resolved"] == {} and out["resubscribed"] is True
    assert hub._livefeed.refreshes == 1                     # still rebuilds the socket
