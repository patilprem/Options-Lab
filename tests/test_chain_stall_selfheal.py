"""The chain poller must try to FIX itself, not just describe its own death.

2026-08-03, 12:05 IST: "NOT RECORDING for >15min during market hours:
option_bars, chain_snapshots[BANKNIFTY], chain_snapshots[CRUDEOIL],
chain_snapshots[GOLD], chain_snapshots[NIFTY]" — the sixth freeze of the same
poller, and the same shape as 2026-07-30: the task alive, every cycle
completing, requests going out, the cache simply not moving. Only the four
recorder names are in _chain_only, so a cycle is 4-8 requests and ~20 seconds;
there is no queue for them to be stuck behind.

Every previous fix made the failure more VISIBLE (top-level guard, no-op
logging, raw-response logging, a per-underlying watchdog push). None of them
recovered anything, so each incident still cost a session of MCX chain data
that can never be re-fetched. The feed has had a self-heal since 07-27 for
exactly this reason; the chain poller now gets the same treatment, running the
two remedies that have actually resolved past incidents (rebuild the client,
drop the cached expiry list) before the 15-minute push, and escalating with
the security id / segment / expiry list in the line when neither works.

These tests pin the escalation ladder, the one-action-per-episode rule, the
session gating that keeps it quiet overnight, and the holiday discriminator.
"""

from datetime import datetime, timedelta

import pytest

from app.engines.paper import (CHAIN_STALL_CLIENT_S, CHAIN_STALL_ESCALATE_S,
                               CHAIN_STALL_EXPIRY_S, MarketHub,
                               chain_stall_stage)

# A Monday inside both the NSE and MCX sessions, well past the open grace.
OPEN = datetime(2026, 8, 3, 12, 5)
CLOSED = datetime(2026, 8, 3, 3, 0)


def _hub():
    hub = MarketHub.__new__(MarketHub)
    hub._wanted = {}
    hub._chain_only = {"NIFTY", "BANKNIFTY"}
    hub._chain_cache = {}
    hub._chain_spot = {}
    hub._chain_seen_fp = {}
    hub._chain_moved_at = {}
    hub._chain_watch_since = {}
    hub._chain_heal_stage = {}
    hub._chain_client_dirty = False
    hub._expiries_cache = {}
    hub._expiries_fail = {}
    hub._expiry_warned = set()
    hub._noop_warned = {}
    return hub


class _Q:
    """Minimal OptionQuote stand-in — _chain_fingerprint only reads ltp/oi."""

    def __init__(self, ltp, oi=0.0):
        self.ltp, self.oi = ltp, oi


# --- the pure ladder --------------------------------------------------------

@pytest.mark.parametrize("stalled,stage", [
    (0.0, 0),
    (CHAIN_STALL_CLIENT_S - 1, 0),
    (CHAIN_STALL_CLIENT_S, 1),
    (CHAIN_STALL_EXPIRY_S - 1, 1),
    (CHAIN_STALL_EXPIRY_S, 2),
    (CHAIN_STALL_ESCALATE_S - 1, 2),
    (CHAIN_STALL_ESCALATE_S, 3),
    (CHAIN_STALL_ESCALATE_S * 10, 3),
])
def test_stage_ladder(stalled, stage):
    assert chain_stall_stage(stalled) == stage


# --- movement tracking ------------------------------------------------------

def test_a_moving_chain_is_never_actioned():
    hub = _hub()
    hub._chain_cache["NIFTY"] = {("WEEKLY", 0, 0, "CE"): _Q(100.0)}
    now = OPEN
    for i in range(20):                      # 20 minutes of a live chain
        hub._chain_cache["NIFTY"][("WEEKLY", 0, 0, "CE")] = _Q(100.0 + i)
        assert hub._chain_stall_step(now, ["NIFTY"]) == []
        now += timedelta(minutes=1)
    assert hub._chain_client_dirty is False


def test_recovery_rearms_the_ladder():
    """A name that comes back and dies again must escalate from stage 1 again,
    not resume where it left off — otherwise the second episode of the day
    skips both remedies and goes straight to a shrug."""
    hub = _hub()
    hub._chain_cache["NIFTY"] = {("WEEKLY", 0, 0, "CE"): _Q(100.0)}
    hub._chain_stall_step(OPEN, ["NIFTY"])
    assert hub._chain_stall_step(
        OPEN + timedelta(seconds=CHAIN_STALL_CLIENT_S), ["NIFTY"]) == \
        [("NIFTY", 1)]
    # it moves again -> episode over
    hub._chain_cache["NIFTY"][("WEEKLY", 0, 0, "CE")] = _Q(101.0)
    later = OPEN + timedelta(seconds=CHAIN_STALL_CLIENT_S + 60)
    assert hub._chain_stall_step(later, ["NIFTY"]) == []
    assert hub._chain_heal_stage.get("NIFTY", 0) == 0
    # ...and freezes again: back to stage 1, not stage 2
    assert hub._chain_stall_step(
        later + timedelta(seconds=CHAIN_STALL_CLIENT_S), ["NIFTY"]) == \
        [("NIFTY", 1)]


# --- the remedies ----------------------------------------------------------

def test_stage_1_asks_the_poll_loop_for_a_fresh_client():
    hub = _hub()
    hub._chain_cache["NIFTY"] = {("WEEKLY", 0, 0, "CE"): _Q(100.0)}
    hub._chain_stall_step(OPEN, ["NIFTY"])
    assert hub._chain_client_dirty is False
    hub._chain_stall_step(OPEN + timedelta(seconds=CHAIN_STALL_CLIENT_S),
                          ["NIFTY"])
    assert hub._chain_client_dirty is True


def test_stage_1_clears_the_noop_log_throttle():
    """Acting is pointless if the result is swallowed: the next poll's failure
    reason must be logged immediately, not up to 5 minutes later."""
    hub = _hub()
    hub._chain_cache["NIFTY"] = {("WEEKLY", 0, 0, "CE"): _Q(100.0)}
    hub._noop_warned = {("NIFTY", "fetch[WEEKLY+0]"): 1.0,
                        ("INFY", "fetch[MONTHLY+0]"): 1.0}
    hub._chain_stall_step(OPEN, ["NIFTY"])
    hub._chain_stall_step(OPEN + timedelta(seconds=CHAIN_STALL_CLIENT_S),
                          ["NIFTY"])
    assert ("NIFTY", "fetch[WEEKLY+0]") not in hub._noop_warned
    assert ("INFY", "fetch[MONTHLY+0]") in hub._noop_warned   # untouched


def test_stage_2_drops_the_cached_expiry_list():
    """The 2026-07-23..27 root cause: a bad expiry list makes every poll a
    no-op over an empty target set, and the cache is keyed by date so it never
    refetches on its own."""
    hub = _hub()
    hub._chain_cache["NIFTY"] = {("WEEKLY", 0, 0, "CE"): _Q(100.0)}
    hub._expiries_cache["NIFTY"] = (OPEN.date(), ["2026-08-04"])
    hub._expiries_fail["NIFTY"] = 123.0
    hub._expiry_warned.add("NIFTY")
    hub._chain_stall_step(OPEN, ["NIFTY"])
    hub._chain_stall_step(OPEN + timedelta(seconds=CHAIN_STALL_EXPIRY_S),
                          ["NIFTY"])
    assert "NIFTY" not in hub._expiries_cache
    assert "NIFTY" not in hub._expiries_fail
    assert "NIFTY" not in hub._expiry_warned


def test_each_stage_fires_once_per_episode():
    """A name dark all day costs three log lines, not one a minute."""
    hub = _hub()
    hub._chain_cache["NIFTY"] = {("WEEKLY", 0, 0, "CE"): _Q(100.0)}
    hub._chain_stall_step(OPEN, ["NIFTY"])
    stages = []
    for m in range(1, 61):
        stages += [s for _, s in
                   hub._chain_stall_step(OPEN + timedelta(minutes=m), ["NIFTY"])]
    assert stages == [1, 2, 3]


def test_escalation_beats_the_watchdog_push():
    """The whole point of the ladder: a human hears about it with a diagnosis
    attached BEFORE the 15-minute 'NOT RECORDING' push, not after."""
    from app.engines.recording_watchdog import STALE_AFTER_S
    assert CHAIN_STALL_ESCALATE_S < STALE_AFTER_S


# --- staying quiet ---------------------------------------------------------

def test_a_closed_session_is_not_a_stall():
    hub = _hub()
    hub._chain_cache["NIFTY"] = {("WEEKLY", 0, 0, "CE"): _Q(100.0)}
    for m in range(0, 120, 5):
        assert hub._chain_stall_step(CLOSED + timedelta(minutes=m),
                                     ["NIFTY"]) == []
    assert hub._chain_client_dirty is False


def test_the_overnight_gap_does_not_escalate_on_the_first_open_pass():
    """Yesterday's last movement must not read as a 20-hour stall the moment
    the session reopens — the poller deserves the full ladder from scratch."""
    hub = _hub()
    hub._chain_cache["NIFTY"] = {("WEEKLY", 0, 0, "CE"): _Q(100.0)}
    hub._chain_stall_step(OPEN - timedelta(days=1), ["NIFTY"])   # yesterday
    hub._chain_stall_step(CLOSED, ["NIFTY"])                     # overnight
    assert hub._chain_stall_step(OPEN, ["NIFTY"]) == []          # reopen
    assert hub._chain_stall_step(
        OPEN + timedelta(seconds=CHAIN_STALL_CLIENT_S), ["NIFTY"]) == \
        [("NIFTY", 1)]


def test_a_name_that_never_cached_anything_still_escalates():
    """The worst case — nothing was ever polled for it (an MCX contract whose
    id never resolved) — has no fingerprint to compare and used to be the one
    the alerting could say least about."""
    hub = _hub()
    hub._chain_stall_step(OPEN, ["CRUDEOIL"])
    out = hub._chain_stall_step(
        OPEN + timedelta(seconds=CHAIN_STALL_ESCALATE_S), ["CRUDEOIL"])
    assert out == [("CRUDEOIL", 3)]
    lvl, _, msg = hub._chain_dead_event("CRUDEOIL", 9, OPEN)
    assert "no entry in UNDERLYINGS" in msg


# --- the holiday discriminator ---------------------------------------------

def test_a_selective_failure_is_an_error():
    """Stocks still ticking proves the market is open and the token is good,
    so four dead index/commodity chains are a real fault worth waking someone
    for — the 07-31 and 08-03 signature exactly."""
    hub = _hub()
    hub._chain_cache["NIFTY"] = {("WEEKLY", 0, 0, "CE"): _Q(100.0)}
    hub._chain_cache["INFY"] = {("MONTHLY", 0, 0, "CE"): _Q(50.0)}
    hub._chain_stall_step(OPEN, ["NIFTY"])
    dead = OPEN + timedelta(seconds=CHAIN_STALL_ESCALATE_S)
    hub._chain_cache["INFY"][("MONTHLY", 0, 0, "CE")] = _Q(51.0)   # still live
    hub._note_chain_movement(dead)
    lvl, src, msg = hub._chain_dead_event("NIFTY", 9, dead)
    assert lvl == "error"
    assert "selective" in msg


def test_a_market_wide_freeze_is_only_a_warning():
    """Nothing moving anywhere is far more likely a holiday than four
    simultaneous per-symbol faults. Don't push a red alert for a Monday off."""
    hub = _hub()
    hub._chain_cache["NIFTY"] = {("WEEKLY", 0, 0, "CE"): _Q(100.0)}
    hub._chain_cache["INFY"] = {("MONTHLY", 0, 0, "CE"): _Q(50.0)}
    hub._chain_stall_step(OPEN, ["NIFTY"])
    dead = OPEN + timedelta(seconds=CHAIN_STALL_ESCALATE_S)
    lvl, src, msg = hub._chain_dead_event("NIFTY", 9, dead)
    assert lvl == "warn"
    assert "NO chain is moving anywhere" in msg


def test_the_dead_event_carries_what_a_human_needs():
    hub = _hub()
    hub._chain_cache["NIFTY"] = {("WEEKLY", 0, 0, "CE"): _Q(100.0)}
    hub._expiries_cache["NIFTY"] = (OPEN.date(), ["2026-08-04", "2026-08-11"])
    _, _, msg = hub._chain_dead_event("NIFTY", 9, OPEN)
    for needle in ("sid=", "seg=", "expiries=", "2026-08-04", "cached_quotes=1"):
        assert needle in msg


# --- the poll loop honours the flag ----------------------------------------

def test_the_poll_loop_drops_its_client_when_asked():
    """The remedy is only real if _poll_cycle actually rebuilds. It owns the
    client's lifetime (a local threaded through the loop), so the heal can
    only raise a flag — this pins that the flag is read and cleared."""
    import asyncio

    hub = _hub()
    hub._chain_only = set()               # nothing to poll; we only want the
    hub._wanted = {}                      # client-handling prologue
    hub._chain_client_dirty = True
    out = asyncio.run(hub._poll_cycle("stale-client", None, 0, set()))
    assert out is None
    assert hub._chain_client_dirty is False
