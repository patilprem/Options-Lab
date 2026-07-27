"""
MarketHub._get_expiries must never cache an EMPTY expiry list for the day.

Root cause of the 2026-07-23..27 index-chain outage: the expiry cache is
keyed by date, so at 00:00 IST it invalidates and the ~1/s chain poll loop
immediately refetches -- during dead hours, when Dhan returns no expiries.
That `[]` was cached for the WHOLE following trading day, so every poll became
a no-op (iterating an empty targets list) and the chain cache never moved.
Nothing was logged, because _poll_one_chain's silent `continue` paths look
identical to success. chain_snapshots stayed empty for NIFTY/BANKNIFTY/
CRUDEOIL/GOLD for days while stock chains recorded fine -- stocks escaped it
because the scanner only fetches their expiries during market hours.
"""

from __future__ import annotations

import asyncio

from app.data.store import SyntheticStore
from app.engines.paper import MarketHub

CFG = {"security_id": 13, "segment": "IDX_I"}


class _Client:
    """expiry_list returns whatever the test queues, counting calls."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def expiry_list(self, **kw):
        self.calls += 1
        return self.responses.pop(0) if self.responses else {"data": {"data": []}}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Loop:
    """Stand-in for the asyncio loop's run_in_executor (runs inline)."""
    async def run_in_executor(self, _executor, fn):
        return fn()


def test_empty_expiry_list_is_not_cached_for_the_day():
    """The regression itself: an empty first response must NOT be cached --
    a later successful fetch has to be able to populate it the same day."""
    hub = MarketHub(SyntheticStore())
    hub.EXPIRY_RETRY_S = 0.0          # no backoff wait in the test
    client = _Client([{"data": {"data": []}},
                      {"data": {"data": ["2026-07-28", "2026-08-04"]}}])

    first = _run(hub._get_expiries(client, "NIFTY", CFG, _Loop()))
    assert first == []
    assert "NIFTY" not in hub._expiries_cache      # <- the fix

    second = _run(hub._get_expiries(client, "NIFTY", CFG, _Loop()))
    assert second == ["2026-07-28", "2026-08-04"]
    assert hub._expiries_cache["NIFTY"][1] == second


def test_successful_list_is_cached_and_not_refetched():
    hub = MarketHub(SyntheticStore())
    client = _Client([{"data": {"data": ["2026-07-28"]}}])

    a = _run(hub._get_expiries(client, "NIFTY", CFG, _Loop()))
    b = _run(hub._get_expiries(client, "NIFTY", CFG, _Loop()))

    assert a == b == ["2026-07-28"]
    assert client.calls == 1           # served from cache the second time


def test_empty_result_backs_off_instead_of_hammering():
    """The poll loop runs ~1/s; an empty list must not mean an API call per
    pass. Within EXPIRY_RETRY_S the call is skipped entirely."""
    hub = MarketHub(SyntheticStore())
    hub.EXPIRY_RETRY_S = 300.0
    client = _Client([{"data": {"data": []}}])

    _run(hub._get_expiries(client, "NIFTY", CFG, _Loop()))
    _run(hub._get_expiries(client, "NIFTY", CFG, _Loop()))
    _run(hub._get_expiries(client, "NIFTY", CFG, _Loop()))

    assert client.calls == 1           # backed off, didn't re-hit


def test_recovery_after_backoff_clears_the_warning_latch():
    hub = MarketHub(SyntheticStore())
    hub.EXPIRY_RETRY_S = 0.0
    client = _Client([{"data": {"data": []}},
                      {"data": {"data": ["2026-07-28"]}}])

    _run(hub._get_expiries(client, "NIFTY", CFG, _Loop()))
    assert "NIFTY" in hub._expiry_warned          # warned once
    _run(hub._get_expiries(client, "NIFTY", CFG, _Loop()))
    assert "NIFTY" not in hub._expiry_warned      # cleared, so a future
    assert "NIFTY" not in hub._expiries_fail      # outage warns again
