"""_poll_one_chain's three silent no-op paths must log — they used to be
completely invisible.

2026-07-29 09:32-09:47+: NIFTY/BANKNIFTY/CRUDEOIL/GOLD chain recording froze
for 15+ minutes with ZERO exceptions logged anywhere. The poll loop was
alive, every cycle completed normally, and the cache simply never moved.
Checked the events table for the whole window: nothing. Root cause is one of
three branches in _poll_one_chain that return early on purpose (resolve_expiry
finding no match, Dhan's message-less blip, or a chain normalizing to zero
quotes) without logging anything — by design, since they can fire on every
~1s poll pass and logging every one would flood the log. But "throttled" and
"silent forever" are different things, and this was the second live incident
through the same unlogged gap in two days.
"""

import asyncio
import time

import pytest

from app.engines.paper import MarketHub


def _hub():
    hub = MarketHub.__new__(MarketHub)
    hub._chain_cache = {}
    hub._chain_spot = {}
    hub._noop_warned = {}
    return hub


def _patch_common(monkeypatch, hub, expiries=("2026-08-06",)):
    async def fake_expiries(*a, **k):
        return list(expiries)

    monkeypatch.setattr(hub, "_get_expiries", fake_expiries)
    events = []
    monkeypatch.setattr("app.core.registry.record_event",
                        lambda *a, **k: events.append(a))
    return events


def _call(hub, targets=(("WEEKLY", 0),), underlying="NIFTY"):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(hub._poll_one_chain(
            underlying, {"security_id": 1}, "client", loop, targets))
    finally:
        loop.close()


# --- each silent path now logs ----------------------------------------------

def test_no_matching_expiry_logs(monkeypatch):
    hub = _hub()
    events = _patch_common(monkeypatch, hub)
    monkeypatch.setattr("app.engines.chain.resolve_expiry", lambda *a: None)
    _call(hub)
    msgs = [a[2] for a in events]
    assert any("resolve_expiry" in m for m in msgs), msgs


def test_message_less_blip_logs(monkeypatch):
    hub = _hub()
    events = _patch_common(monkeypatch, hub)
    monkeypatch.setattr("app.engines.chain.resolve_expiry",
                        lambda *a: "2026-08-06")

    async def fake_fetch(*a, **k):
        return None

    monkeypatch.setattr(hub, "_fetch_chain_ratelimited", fake_fetch)
    _call(hub)
    msgs = [a[2] for a in events]
    assert any("fetch[" in m for m in msgs), msgs


def test_zero_quotes_after_normalize_logs(monkeypatch):
    hub = _hub()
    events = _patch_common(monkeypatch, hub)
    monkeypatch.setattr("app.engines.chain.resolve_expiry",
                        lambda *a: "2026-08-06")

    async def fake_fetch(*a, **k):
        return {"some": "data"}

    monkeypatch.setattr(hub, "_fetch_chain_ratelimited", fake_fetch)
    monkeypatch.setattr("app.engines.chain.normalize_chain", lambda *a, **k: {})
    _call(hub)
    msgs = [a[2] for a in events]
    assert any("normalize[" in m for m in msgs), msgs
    assert hub._chain_cache.get("NIFTY", {}) == {}, "must not fabricate quotes"


def test_a_real_update_logs_nothing(monkeypatch):
    """The common, healthy case must stay silent — this is a diagnostic for
    the failure mode, not a running commentary on every successful poll."""
    hub = _hub()
    events = _patch_common(monkeypatch, hub)
    monkeypatch.setattr("app.engines.chain.resolve_expiry",
                        lambda *a: "2026-08-06")

    async def fake_fetch(*a, **k):
        return {"data": {"oc": {"100": {"ce": {}}}}}

    monkeypatch.setattr(hub, "_fetch_chain_ratelimited", fake_fetch)
    monkeypatch.setattr("app.engines.chain.normalize_chain",
                        lambda *a, **k: {("WEEKLY", 0, 0, "CALL"): object()})
    monkeypatch.setattr("app.engines.chain.chain_spot", lambda d: None)
    _call(hub)
    assert events == []


# --- throttling: the whole reason this wasn't just "log everything" --------

def test_repeated_noop_is_throttled_not_flooded(monkeypatch):
    hub = _hub()
    events = _patch_common(monkeypatch, hub)
    monkeypatch.setattr("app.engines.chain.resolve_expiry", lambda *a: None)
    for _ in range(20):
        _call(hub)
    assert len(events) == 1, (
        f"expected exactly one throttled log line, got {len(events)} — "
        f"repeated no-ops at ~1s cadence must not flood the events table")


def test_throttle_releases_after_the_window(monkeypatch):
    hub = _hub()
    events = _patch_common(monkeypatch, hub)
    monkeypatch.setattr("app.engines.chain.resolve_expiry", lambda *a: None)
    _call(hub)
    assert len(events) == 1
    hub._noop_warned[("NIFTY", "resolve_expiry[WEEKLY+0]")] = (
        time.monotonic() - hub._NOOP_WARN_S - 1)
    _call(hub)
    assert len(events) == 2


def test_different_underlyings_throttle_independently(monkeypatch):
    """The 2026-07-29 incident hit FOUR underlyings at once — each must get
    its own throttle slot, not share one and mask the others."""
    hub = _hub()
    events = _patch_common(monkeypatch, hub)
    monkeypatch.setattr("app.engines.chain.resolve_expiry", lambda *a: None)
    for u in ("NIFTY", "BANKNIFTY", "CRUDEOIL", "GOLD"):
        _call(hub, underlying=u)
    assert len(events) == 4, "each underlying should log once independently"
