"""A chain-dead alert must name the actual fault, and one it can name is the
TOKEN.

2026-08-24, 09:14 IST, on the phone:

    chain DEAD [GOLD] 9min — client rebuild AND expiry refetch both failed
    while ticks are still arriving, so the market is live and this is a real
    fault; sid=483079 seg=MCX_COMM targets=(('WEEKLY', 0), ('WEEKLY', 1))
    expiries=None cached_quotes=756

Three things in that one line are wrong or unhelpable:

  * `targets=(('WEEKLY', 0), ('WEEKLY', 1))` — GOLD's options are monthly-only
    and the poller remaps them to MONTHLY. The line printed the raw class
    constant, which is exactly the hardcoded-("WEEKLY", 0) bug CLAUDE.md warns
    about, and it sends the reader hunting a relabel regression that isn't
    there.
  * `expiries=None` — silent about WHY. "The poller never reached this name"
    and "expiry_list keeps answering EMPTY" have different causes and
    different fixes, and only the second explains why the ladder's stage-2
    remedy changed nothing (an empty list is never cached, so there was
    nothing to drop).
  * "the market is live and this is a real fault" — the discriminator asks
    whether ticks are arriving, and an expired 24h token does not stop them:
    the MarketFeed socket authenticated when it connected and keeps streaming,
    while every REST call dies. A weekend-expired token gets exactly ONE
    login-link push at 08:30 Monday, and if that scrolls past, the whole
    session is dead with the alerts blaming the chain. The fix is a 20-second
    phone tap; the alert sent someone to the VPS.

These tests pin the credential check, the re-push, and the three diagnostic
fields.
"""

from datetime import datetime, timedelta

import pytest

from app.core import token_manager
from app.data import dhan_client
from app.engines.paper import MarketHub

OPEN = datetime(2026, 8, 24, 9, 14)      # the Monday, at the alert's minute


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    """A developer's shell may have real Dhan env vars; these tests decide
    the credential world themselves."""
    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("DHAN_CONFIG_PATH", raising=False)


def _hub(tick_age=2.0):
    hub = MarketHub.__new__(MarketHub)
    hub.feed_status = lambda: {"mode": "live", "connected": True,
                               "tick_age_sec": tick_age}
    hub._chain_cache = {}
    hub._chain_spot = {}
    hub._chain_seen_fp = {}
    hub._chain_moved_at = {}
    hub._chain_watch_since = {}
    hub._chain_heal_stage = {}
    hub._chain_retry_at = {}
    hub._chain_client_dirty = False
    hub._expiries_cache = {}
    hub._expiries_fail = {}
    hub._expiry_warned = set()
    hub._noop_warned = {}
    return hub


@pytest.fixture
def gold(monkeypatch):
    """GOLD as the alert reported it: an MCX contract with cached quotes."""
    monkeypatch.setitem(dhan_client.UNDERLYINGS, "GOLD",
                        {"security_id": 483079, "segment": "MCX_COMM",
                         "fno_segment": "MCX_COMM", "instrument": "OPTFUT"})


def _save_token(expires_in_h: float):
    token_manager._init()
    now = datetime.now(token_manager.IST)
    with token_manager._conn() as c:
        c.execute("INSERT OR REPLACE INTO dhan_token VALUES (1, ?, ?, ?)",
                  ("jwt", now.isoformat(),
                   (now + timedelta(hours=expires_in_h)).isoformat()))


# --- which credential is in play -------------------------------------------

def test_an_env_token_is_never_blamed(monkeypatch):
    """Nothing local can validate a static env token, so the honest answer is
    `unknown` — and an unknown credential must never be called dead. A false
    'your token expired' sends someone to fix the one thing that works."""
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "jwt")
    h = dhan_client.credential_health()
    assert (h["source"], h["state"]) == ("env", "unknown")
    assert dhan_client.credential_is_dead() is False


def test_an_env_token_wins_over_a_dead_managed_one(monkeypatch):
    """resolve_credentials() prefers env, so the managed token's state is
    irrelevant on a box that sets one. Reporting it would be a confident wrong
    answer."""
    _save_token(-1)                              # managed token long expired
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "jwt")
    assert dhan_client.credential_is_dead() is False


def test_a_live_managed_token_is_healthy():
    _save_token(24)
    h = dhan_client.credential_health()
    assert (h["source"], h["state"]) == ("managed", "ok")
    assert dhan_client.credential_is_dead() is False


def test_an_expired_managed_token_is_dead():
    _save_token(-1)
    assert dhan_client.credential_health()["state"] == "expired"
    assert dhan_client.credential_is_dead() is True


def test_no_credential_at_all_is_dead():
    assert dhan_client.credential_health()["state"] == "missing"
    assert dhan_client.credential_is_dead() is True


# --- the dead-token branch of the alert ------------------------------------

def test_a_dead_token_is_not_reported_as_a_dead_chain(gold, monkeypatch):
    """THE 2026-08-24 CASE. Ticks arriving (the socket authenticated before
    the token expired), every REST call failing. The old line concluded 'the
    market is live and this is a real fault' and named the chain."""
    monkeypatch.setattr(token_manager, "repush_login_link", lambda reason="": True)
    _save_token(-1)
    hub = _hub(tick_age=2.0)
    lvl, src, msg = hub._chain_dead_event("GOLD", 9, OPEN)
    assert (lvl, src) == ("error", "token")
    assert "TOKEN IS DEAD" in msg
    assert "real fault" not in msg
    assert "token=managed:expired" in msg


def test_the_dead_token_alert_says_a_client_rebuild_cannot_help(gold, monkeypatch):
    """The ladder has just spent stages 1 and 2 on this. Whoever reads the
    alert needs to know why they did nothing: a fresh client is built from the
    same dead token."""
    monkeypatch.setattr(token_manager, "repush_login_link", lambda reason="": True)
    _save_token(-1)
    _, _, msg = _hub()._chain_dead_event("GOLD", 9, OPEN)
    assert "rebuilds cannot help" in msg


def test_a_dead_token_re_pushes_the_login_link(gold, monkeypatch):
    """The 08:30 push fires ONCE a day. A token that expired over the weekend
    gets one notification before the open and nothing after it — so the thing
    that NOTICES the outage has to hand over something tappable."""
    asked = []
    monkeypatch.setattr(token_manager, "repush_login_link",
                        lambda reason="": asked.append(reason) or True)
    _save_token(-1)
    _hub()._chain_dead_event("GOLD", 9, OPEN)
    assert asked and "GOLD" in asked[0]


def test_a_healthy_token_still_blames_the_chain(gold, monkeypatch):
    """The narrow branch must stay narrow: with a good token, the live-market
    discriminator is still the one that decides."""
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "jwt")
    lvl, src, msg = _hub(tick_age=2.0)._chain_dead_event("GOLD", 9, OPEN)
    assert (lvl, src) == ("error", "feed")
    assert "real fault" in msg


def test_a_failing_login_push_cannot_kill_the_alert(gold, monkeypatch):
    """Same rule as _push_chain_alert: the notifier is the least important
    part of reporting a failure."""
    def boom(reason=""):
        raise RuntimeError("ntfy down")
    monkeypatch.setattr(token_manager, "repush_login_link", boom)
    _save_token(-1)
    lvl, _, msg = _hub()._chain_dead_event("GOLD", 9, OPEN)
    assert lvl == "error" and "TOKEN IS DEAD" in msg


def test_the_login_re_push_is_throttled(monkeypatch):
    """Per-underlying alerting means this can be reached four times in a
    minute; four identical links is a push storm, not a notification."""
    sent = []
    monkeypatch.setattr(token_manager, "build_login_url", lambda: "https://x")
    monkeypatch.setattr(token_manager, "NTFY_TOPIC", "topic")
    monkeypatch.setattr(token_manager, "_last_repush", 0.0)
    monkeypatch.setattr(token_manager.requests, "post",
                        lambda *a, **k: sent.append(k.get("data")))
    assert token_manager.repush_login_link("first") is True
    assert token_manager.repush_login_link("second") is False
    assert len(sent) == 1


# --- the three diagnostic fields -------------------------------------------

def test_the_detail_prints_the_targets_actually_polled(gold):
    """GOLD's alert said WEEKLY. Its options are monthly-only and the poller
    knows it — the line must not re-introduce the hardcoded expiry kind the
    relabel fix removed everywhere else."""
    hub = _hub()
    hub._expiries_cache["GOLD"] = (OPEN.date(), ["2026-08-31", "2026-09-29"])
    detail = hub._chain_detail("GOLD")
    assert "MONTHLY" in detail
    assert "WEEKLY" not in detail


def test_a_real_weekly_underlying_still_prints_weekly(monkeypatch):
    monkeypatch.setitem(dhan_client.UNDERLYINGS, "NIFTY",
                        {"security_id": 13, "segment": "IDX_I"})
    hub = _hub()
    hub._expiries_cache["NIFTY"] = (OPEN.date(), ["2026-08-25", "2026-09-01"])
    assert "WEEKLY" in hub._chain_detail("NIFTY")


def test_unknowable_targets_are_marked_unresolved(gold):
    """With no expiry list and no live verdict, the poller has not chosen yet.
    Saying so IS the finding; guessing is how the last line misled."""
    assert "unresolved" in _hub()._chain_detail("GOLD")


def test_an_empty_expiry_list_is_distinguished_from_never_asking(gold):
    """`expiries=None` covered both. Only one of them says the failure is
    UPSTREAM of option_chain — and explains why stage 2 was a no-op."""
    hub = _hub()
    assert "never fetched" in hub._chain_detail("GOLD")

    import time
    hub._expiries_fail["GOLD"] = time.monotonic() - 300
    detail = hub._chain_detail("GOLD")
    assert "answered EMPTY" in detail
    assert "no cached list to drop" in detail


def test_every_chain_detail_carries_the_token_state(gold):
    """Whatever the verdict, the reader should not have to ask separately."""
    _save_token(24)
    assert "token=managed:ok" in _hub()._chain_detail("GOLD")


# --- an expiry-list outage must not poison the weekly-cycle verdict --------

def test_an_empty_expiry_list_records_no_weekly_verdict(monkeypatch):
    """has_weekly_cycle answers False for an empty list — fine for remapping
    targets, wrong to REMEMBER. _effective_leg re-labels a running strategy's
    legs from this fact, so an expiry_list outage would send a live NIFTY
    strategy looking up MONTHLY keys against a WEEKLY-keyed cache."""
    import asyncio

    hub = _hub()
    hub._chain_no_weekly = set()
    monkeypatch.setitem(dhan_client.UNDERLYINGS, "NIFTY",
                        {"security_id": 13, "segment": "IDX_I"})

    async def no_expiries(*a, **k):
        return []
    monkeypatch.setattr(MarketHub, "_get_expiries", no_expiries)

    asyncio.run(hub._poll_one_chain(
        "NIFTY", dhan_client.UNDERLYINGS["NIFTY"], object(),
        asyncio.new_event_loop(), (("WEEKLY", 0),)))
    assert hub.no_weekly_cycle("NIFTY") is False
