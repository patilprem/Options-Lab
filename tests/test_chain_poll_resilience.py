"""The chain poll loop must never die silently.

2026-07-28: all four chain caches froze within 15 seconds of each other at
09:30 — the minute the Tier-2 scanner first calls enable_chain() and mutates
the _chain_only set that _chain_order() iterates. The feed stayed connected,
underlying_bars kept writing, the dashboard stayed green, and chain recording
was simply gone until someone read the log 20 minutes later.

The per-underlying try/except covered a single name's fetch. _chain_order()
was evaluated OUTSIDE it, in the for statement, so an exception there killed
the task outright: no restart, no event, no symptom except rows quietly
stopping. These tests pin both halves of the fix — the snapshot in
_chain_order, and the top-level guard that survives anything it misses.
"""

import asyncio

import pytest

from app.engines.paper import MarketHub


def _hub():
    hub = MarketHub.__new__(MarketHub)
    hub._wanted = {}
    hub._chain_only = set()
    return hub


# --- _chain_order: the likely trigger ---------------------------------------

def test_order_puts_deployed_names_first():
    hub = _hub()
    hub._wanted = {"NIFTY": {5}, "BANKNIFTY": {5}}
    hub._chain_only = {"INFY", "TCS"}
    order = hub._chain_order()
    assert set(order[:2]) == {"NIFTY", "BANKNIFTY"}
    assert set(order[2:]) == {"INFY", "TCS"}


def test_a_name_in_both_is_not_polled_twice():
    hub = _hub()
    hub._wanted = {"NIFTY": {5}}
    hub._chain_only = {"NIFTY", "INFY"}
    assert sorted(hub._chain_order()) == ["INFY", "NIFTY"]


def test_survives_the_set_being_mutated_while_it_copies():
    """THE trigger. A set that mutates during iteration raises RuntimeError;
    the poll loop must get a list back, not an exception."""
    hub = _hub()

    class Mutating(set):
        def __init__(self, *a):
            super().__init__(*a)
            self.calls = 0

        def __iter__(self):
            self.calls += 1
            if self.calls == 1:          # first copy races, then settles
                raise RuntimeError("Set changed size during iteration")
            return super().__iter__()

    hub._chain_only = Mutating({"INFY"})
    assert hub._chain_order() == ["INFY"]


def test_gives_up_quietly_if_it_never_settles():
    """An empty pass is survivable — the next cycle is a second away. A raised
    exception was not."""
    hub = _hub()

    class AlwaysMutating(set):
        def __iter__(self):
            raise RuntimeError("Set changed size during iteration")

    hub._chain_only = AlwaysMutating({"INFY"})
    assert hub._chain_order() == []


def test_empty_is_fine():
    assert _hub()._chain_order() == []


# --- the top-level guard ----------------------------------------------------

def test_poll_cycle_isolates_one_bad_underlying(monkeypatch):
    """Pre-existing behaviour that must survive the refactor: one failing name
    does not stop the others."""
    hub = _hub()
    hub._wanted = {}
    hub._chain_only = {"GOOD", "BAD"}
    hub.CHAIN_TARGETS = (("WEEKLY", 0),)
    hub.SECONDARY_EVERY = 1
    hub.CHAIN_MIN_INTERVAL = 0
    polled = []

    async def fake_poll(u, cfg, client, loop, targets):
        if u == "BAD":
            raise RuntimeError("chain fetch blew up")
        polled.append(u)

    monkeypatch.setattr(hub, "_poll_one_chain", fake_poll)
    monkeypatch.setattr("app.engines.paper.UNDERLYINGS",
                        {"GOOD": {"security_id": 1}, "BAD": {"security_id": 2}})
    # A failure drops the client, so the NEXT name rebuilds one — that rebuild
    # must not reach the real SDK (no creds offline, and it would mask the
    # isolation this test is checking).
    monkeypatch.setattr("app.data.dhan_client.get_client", lambda: "client")
    events = []
    monkeypatch.setattr("app.core.registry.record_event",
                        lambda *a, **k: events.append(a))

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(hub._poll_cycle("client", loop, 0, set()))
    finally:
        loop.close()
    assert polled == ["GOOD"]


def test_poll_cycle_raising_is_reported_not_fatal(monkeypatch):
    """What actually killed the task: _chain_order() itself raising. The guard
    lives in _chain_poll_loop, so here we assert _poll_cycle propagates (so the
    guard can see it) rather than swallowing it invisibly."""
    hub = _hub()

    def boom():
        raise RuntimeError("Set changed size during iteration")

    monkeypatch.setattr(hub, "_chain_order", boom)
    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(RuntimeError):
            loop.run_until_complete(hub._poll_cycle(None, loop, 0, set()))
    finally:
        loop.close()


# --- _watch_segments: the same hazard, on the watchdog's path ---------------

def test_watch_segments_survives_the_set_being_mutated_while_it_copies(monkeypatch):
    """Same fault as _chain_order, different victim. _watch_segments feeds the
    once-a-minute watchdog loop, LiveFeed's watch_open callback, and
    feed_status() — which the chain self-heal reads to tell a real outage from
    a holiday. A RuntimeError here takes down the safety net itself."""
    from app.data.dhan_client import UNDERLYINGS
    # MCX names only exist in UNDERLYINGS once resolve_mcx_ids has injected
    # them at runtime; do the same here so the MCX branch is actually reached.
    monkeypatch.setitem(UNDERLYINGS, "CRUDEOIL",
                        {"security_id": 560977, "segment": "MCX_COMM"})
    hub = _hub()
    hub._wanted = {"NIFTY": {5}}

    class Mutating(set):
        def __init__(self, *a):
            super().__init__(*a)
            self.calls = 0

        def __iter__(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Set changed size during iteration")
            return super().__iter__()

    hub._chain_only = Mutating({"CRUDEOIL"})
    assert hub._watch_segments() == {"NSE", "MCX"}


def test_watch_segments_degrades_to_empty_rather_than_raising():
    """If it never settles, an empty set is survivable — the next pass is a
    minute away. A raise is not."""
    hub = _hub()
    hub._wanted = {}

    class AlwaysMutating(set):
        def __iter__(self):
            raise RuntimeError("Set changed size during iteration")

    hub._chain_only = AlwaysMutating({"GOLD"})
    assert hub._watch_segments() == set()
