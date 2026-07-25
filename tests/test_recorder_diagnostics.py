"""
_market_recorder's silent-skip diagnosis.

Reported symptom (2026-07-24): chain_snapshots had NIFTY/BANKNIFTY/CRUDEOIL/
GOLD only up to 07-23, with a full trading session (07-24) simply missing and
NOTHING in the activity log explaining it. Root cause of the *undiagnosability*
(separate from whatever caused the outage itself): the recorder's frozen-chain
guard did `if not changed: continue` with no logging at all, so two completely
different situations were indistinguishable after the fact —

  * genuine holiday / frozen chain  -> correct to skip, nothing wrong
  * chain poller dead / cache empty -> a real outage losing a day of data

These tests pin the distinguishing signal: MarketHub._chain_fingerprint()
returns None when there is NO cached chain for an underlying (the outage
signature) versus a stable tuple when the chain is merely unchanged (frozen).
The recorder keys its warning off exactly that difference.
"""

from __future__ import annotations

from datetime import date, datetime

from app.core.contract import OptionQuote, OptionType
from app.data.store import SyntheticStore
from app.engines.paper import MarketHub


def _quote(ltp, oi=1000.0):
    return OptionQuote(
        ts=datetime(2026, 7, 24, 10, 0), underlying="NIFTY",
        expiry=date(2026, 7, 28), strike=23800, option_type=OptionType.CALL,
        ltp=ltp, bid=ltp - 0.05, ask=ltp + 0.05, oi=oi)


def _hub():
    return MarketHub(SyntheticStore())


def test_fingerprint_none_when_no_chain_cached():
    """The outage signature: the poller never populated a cache for this
    name, so there is nothing to fingerprint."""
    hub = _hub()
    assert hub._chain_fingerprint("NIFTY") is None


def test_fingerprint_stable_when_chain_frozen():
    """The holiday signature: a cache EXISTS and its content doesn't move, so
    the fingerprint is a stable non-None tuple. Distinguishable from the
    outage case above purely by None-ness — which is what the recorder's
    warning uses to say WHICH of the two happened."""
    hub = _hub()
    hub._chain_cache["NIFTY"] = {("WEEKLY", 0, 0, "CALL"): _quote(120.0)}
    hub._chain_spot["NIFTY"] = 23800.0

    first = hub._chain_fingerprint("NIFTY")
    second = hub._chain_fingerprint("NIFTY")

    assert first is not None
    assert first == second          # frozen -> identical, so chain_changed skips


def test_fingerprint_moves_when_chain_content_changes():
    """A live session: content moves, so the fingerprint differs and
    chain_changed() reports it as worth persisting."""
    hub = _hub()
    hub._chain_cache["NIFTY"] = {("WEEKLY", 0, 0, "CALL"): _quote(120.0)}
    hub._chain_spot["NIFTY"] = 23800.0
    before = hub._chain_fingerprint("NIFTY")

    hub._chain_cache["NIFTY"] = {("WEEKLY", 0, 0, "CALL"): _quote(131.5)}
    after = hub._chain_fingerprint("NIFTY")

    assert before != after


def test_chain_changed_skips_frozen_and_uncached_alike():
    """Both failure modes produce an EMPTY changed-list — which is precisely
    why the recorder couldn't tell them apart before, and why it now has to
    inspect the fingerprints itself to describe the skip."""
    hub = _hub()
    # frozen: cached, already marked persisted at this exact content
    hub._chain_cache["NIFTY"] = {("WEEKLY", 0, 0, "CALL"): _quote(120.0)}
    hub._chain_spot["NIFTY"] = 23800.0
    hub.mark_chain_persisted(["NIFTY"])
    # uncached: nothing was ever polled for BANKNIFTY
    assert hub.chain_changed(["NIFTY", "BANKNIFTY"]) == []

    # the recorder's own split of that empty result, by fingerprint None-ness
    live = ["NIFTY", "BANKNIFTY"]
    cold = [u for u in live if hub._chain_fingerprint(u) is None]
    frozen = sorted(set(live) - set(cold))
    assert cold == ["BANKNIFTY"]        # real outage — no cache at all
    assert frozen == ["NIFTY"]          # benign — cached but unchanged


def test_changed_chain_is_reported_for_persist():
    hub = _hub()
    hub._chain_cache["NIFTY"] = {("WEEKLY", 0, 0, "CALL"): _quote(120.0)}
    hub._chain_spot["NIFTY"] = 23800.0
    hub.mark_chain_persisted(["NIFTY"])

    hub._chain_cache["NIFTY"] = {("WEEKLY", 0, 0, "CALL"): _quote(133.0)}

    assert hub.chain_changed(["NIFTY"]) == ["NIFTY"]
