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
    # _noop_warn is gated on the underlying's session being open. Pin it here
    # rather than letting these tests depend on the wall clock they happen to
    # run at — the gate itself is exercised explicitly further down.
    hub._session_open_for_chain = lambda u: True
    return hub


def _patch_common(monkeypatch, hub,
                  expiries=("2026-08-06", "2026-08-13", "2026-08-20")):
    # A genuine WEEKLY cadence by default: _poll_one_chain now derives the
    # expiry KIND from this list (chain.effective_targets), so a one-entry
    # list would silently re-label these NIFTY cases MONTHLY.
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


# --- expiry KIND is derived from the list (2026-07-29 BANKNIFTY finding) ---
#
# CHAIN_TARGETS asks every underlying for ("WEEKLY", 0/1). BANKNIFTY's weeklies
# are discontinued and MCX is monthly-only, so resolve_expiry answered
# ("WEEKLY", 0) with their nearest MONTHLY contract and we cached, recorded and
# PERSISTED it as WEEKLY — while offset 1 landed ~2 months out. One expiry,
# wrongly labelled, and never a second. The kind now comes from the data.

# A real weekly cadence, and a monthly-only list (BANKNIFTY/MCX shaped).
WEEKLIES = ("2026-08-06", "2026-08-13", "2026-08-20", "2026-08-27")
MONTHLIES = ("2026-08-25", "2026-09-29", "2026-10-27")


def _fetch_recorder(monkeypatch, hub):
    """Capture the expiry each fetch was made for."""
    fetched = []

    async def fake_fetch(u, client, cfg, exp, loop):
        fetched.append(exp)
        return None      # message-less blip; irrelevant to these tests

    monkeypatch.setattr(hub, "_fetch_chain_ratelimited", fake_fetch)
    return fetched


def test_monthly_only_underlying_is_polled_as_monthly(monkeypatch):
    """THE fix. A monthly-only expiry list must re-label WEEKLY targets to
    MONTHLY, so the chain is stored under the kind it actually is AND offset 1
    becomes a real next-month expiry instead of a skipped phantom weekly."""
    hub = _hub()
    _patch_common(monkeypatch, hub, expiries=MONTHLIES)
    fetched = _fetch_recorder(monkeypatch, hub)
    _call(hub, targets=(("WEEKLY", 0), ("WEEKLY", 1)), underlying="BANKNIFTY")
    assert fetched == ["2026-08-25", "2026-09-29"], (
        "both monthly offsets should be fetched, offset 1 being a genuine "
        "next-month expiry rather than a phantom weekly")
    assert hub.no_weekly_cycle("BANKNIFTY") is True


def test_monthly_only_relabel_points_at_the_same_front_contract(monkeypatch):
    """Re-labelling must not RE-POINT: MONTHLY 0 has to resolve to the very
    expiry WEEKLY 0 did, or the fix would silently switch which contract the
    live cache (and every open position marked through it) prices."""
    from app.engines import chain as chainmod
    assert (chainmod.resolve_expiry(list(MONTHLIES), "MONTHLY", 0)
            == chainmod.resolve_expiry(list(MONTHLIES), "WEEKLY", 0))


def test_a_real_weekly_underlying_is_left_alone(monkeypatch):
    """A genuine 7-day cadence must stay WEEKLY — the remap is for lists that
    prove there is no weekly cycle, not for every underlying."""
    hub = _hub()
    _patch_common(monkeypatch, hub, expiries=WEEKLIES)
    fetched = _fetch_recorder(monkeypatch, hub)
    _call(hub, targets=(("WEEKLY", 0), ("WEEKLY", 1)))
    assert fetched == ["2026-08-06", "2026-08-13"]
    assert hub.no_weekly_cycle("NIFTY") is False


def test_a_skipped_holiday_week_is_still_a_weekly_cycle(monkeypatch):
    """A missed week (exchange holiday shifting the cycle) is still a real
    weekly market, just ~14 days out instead of 7 — must not be misread as
    'no weekly cycle exists here' and demoted to MONTHLY."""
    hub = _hub()
    _patch_common(monkeypatch, hub,
                  expiries=("2026-08-05", "2026-08-19", "2026-08-26"))
    fetched = _fetch_recorder(monkeypatch, hub)
    _call(hub, targets=(("WEEKLY", 0), ("WEEKLY", 1)))
    assert fetched == ["2026-08-05", "2026-08-19"]
    assert hub.no_weekly_cycle("NIFTY") is False


def test_monthly_targets_pass_through_unchanged(monkeypatch):
    """The scanner already asks stocks for ("MONTHLY", 0) — an explicit
    MONTHLY target must never be rewritten."""
    hub = _hub()
    _patch_common(monkeypatch, hub, expiries=MONTHLIES)
    fetched = _fetch_recorder(monkeypatch, hub)
    _call(hub, targets=(("MONTHLY", 0), ("MONTHLY", 1)), underlying="INFY")
    assert fetched == ["2026-08-25", "2026-09-29"]


def test_offset_order_does_not_matter(monkeypatch):
    """Targets listed offset>0 first must still resolve both."""
    hub = _hub()
    _patch_common(monkeypatch, hub, expiries=WEEKLIES)
    fetched = _fetch_recorder(monkeypatch, hub)
    _call(hub, targets=(("WEEKLY", 1), ("WEEKLY", 0)))
    assert sorted(fetched) == ["2026-08-06", "2026-08-13"]


def test_a_missing_secondary_expiry_is_a_permanent_fact(monkeypatch):
    """One listed contract means there is no "next" one — correct behaviour,
    reported forever. It must take the DAILY throttle, not the 5-minute one,
    or it becomes the noise that hid the 2026-08-03 outage."""
    hub = _hub()
    events = _patch_common(monkeypatch, hub, expiries=("2026-08-25",))
    _fetch_recorder(monkeypatch, hub)
    _call(hub, targets=(("WEEKLY", 0), ("WEEKLY", 1)), underlying="GOLD")
    reasons = [a[2] for a in events]
    assert any("no-secondary-expiry[MONTHLY+1]" in m for m in reasons), reasons
    assert not any("resolve_expiry[" in m for m in reasons), (
        "offset 0 resolved fine — only the secondary is missing")


# --- permanent facts must not drown the log --------------------------------

def _noop_events(monkeypatch, hub, u, reason, aged_by):
    """Age the throttle entry by `aged_by` seconds, then call _noop_warn and
    return whatever it logged."""
    hub._noop_warned[(u, reason)] = time.monotonic() - aged_by
    events = []
    monkeypatch.setattr("app.core.registry.record_event",
                        lambda *a, **k: events.append(a))
    hub._noop_warn(u, reason, "detail")
    return events


def test_the_missing_secondary_line_is_logged_once_a_day(monkeypatch):
    """2026-08-03: BANKNIFTY/CRUDEOIL/GOLD reported a benign, permanent fact
    about their expiry list at the 5-minute throttle — ~36 lines an hour. The
    last 40 chain events during a live incident were 33 copies of that one line
    and nothing from the outage itself.

    A permanent fact about an underlying is not an incident. Log it once."""
    hub = _hub()
    reason = "no-secondary-expiry[MONTHLY+1]"
    assert _noop_events(monkeypatch, hub, "BANKNIFTY", reason,
                        hub._NOOP_WARN_S + 1) == [], \
        "5 minutes on is still inside the daily window — must stay quiet"
    assert len(_noop_events(monkeypatch, hub, "BANKNIFTY", reason,
                            hub._NOOP_WARN_DAILY_S + 1)) == 1


def test_real_failures_keep_the_short_throttle(monkeypatch):
    """The daily window is ONLY for permanent facts. An empty-failure or a
    fetch no-op is an incident and must still surface every 5 minutes."""
    hub = _hub()
    for reason in ("fetch[WEEKLY+0]", "empty-failure-detail[IDX_I]",
                   "resolve_expiry[WEEKLY+0]", "normalize[WEEKLY+0]"):
        got = _noop_events(monkeypatch, hub, "NIFTY", reason,
                           hub._NOOP_WARN_S + 1)
        assert len(got) == 1, f"{reason} must not be throttled to daily"


# --- off-hours no-ops are the expected answer, not a symptom ----------------

def test_no_op_before_the_open_is_not_logged(monkeypatch):
    """2026-08-04 08:45, pre-open: all four names logged empty-failure warns
    every 5 minutes because Dhan serves no chain before the session.

    That noise is worse than ordinary noise. `empty-failure-detail ...
    raw_response=` is the exact signature of the 08-03 poisoned-client outage,
    and printing it every morning for a benign reason makes the real thing
    indistinguishable from the routine one."""
    hub = _hub()
    monkeypatch.setattr(hub, "_session_open_for_chain", lambda u: False)
    events = []
    monkeypatch.setattr("app.core.registry.record_event",
                        lambda *a, **k: events.append(a))
    hub._noop_warn("NIFTY", "empty-failure-detail[IDX_I]", "raw_response=...")
    assert events == []


def test_the_same_no_op_during_the_session_is_logged(monkeypatch):
    hub = _hub()
    monkeypatch.setattr(hub, "_session_open_for_chain", lambda u: True)
    events = []
    monkeypatch.setattr("app.core.registry.record_event",
                        lambda *a, **k: events.append(a))
    hub._noop_warn("NIFTY", "empty-failure-detail[IDX_I]", "raw_response=...")
    assert len(events) == 1


def test_the_gate_fails_loud_if_it_cannot_tell(monkeypatch):
    """Losing the one line that diagnoses an outage is far worse than printing
    it off-hours, so an unusable session check must not silence anything."""
    hub = _hub()

    def boom(*a, **k):
        raise RuntimeError("no session table")

    monkeypatch.setattr("app.engines.recording_watchdog.segment_for", boom)
    assert hub._session_open_for_chain("NIFTY") is True
