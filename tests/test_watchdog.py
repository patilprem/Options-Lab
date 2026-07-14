"""Feed watchdog decision core — offline, pure.

Alert etiquette under test: push on state change, re-push only after
REALERT_MIN while still broken, one recovery push, silence off-hours and
in synthetic mode.
"""

from datetime import datetime, timedelta

from app.engines.watchdog import (FeedWatchdog, QUIET_AFTER_S, REALERT_MIN,
                                  session_open_for)

NOW = datetime(2026, 7, 15, 11, 0)   # Wednesday, mid-session

LIVE_OK = {"mode": "live", "connected": True, "tick_age_sec": 2.0}
LIVE_QUIET = {"mode": "live", "connected": True, "tick_age_sec": QUIET_AFTER_S + 60}
LIVE_DOWN = {"mode": "live", "connected": False, "tick_age_sec": None}


def collector():
    sent = []
    return sent, lambda msg, kind: sent.append((kind, msg)) or True


def test_healthy_feed_stays_silent():
    sent, notify = collector()
    wd = FeedWatchdog(notify)
    for i in range(10):
        assert wd.step(LIVE_OK, True, NOW + timedelta(minutes=i)) is None
    assert sent == []


def test_down_alerts_once_then_realerts_after_interval():
    sent, notify = collector()
    wd = FeedWatchdog(notify)
    assert wd.step(LIVE_DOWN, True, NOW) == "down"
    # within the re-alert window: silence, not spam
    assert wd.step(LIVE_DOWN, True, NOW + timedelta(minutes=5)) is None
    assert wd.step(LIVE_DOWN, True, NOW + timedelta(minutes=REALERT_MIN - 1)) is None
    # after the window: re-alert
    assert wd.step(LIVE_DOWN, True, NOW + timedelta(minutes=REALERT_MIN)) == "down"
    assert [k for k, _ in sent] == ["down", "down"]


def test_recovery_pushes_all_clear_once():
    sent, notify = collector()
    wd = FeedWatchdog(notify)
    wd.step(LIVE_DOWN, True, NOW)
    assert wd.step(LIVE_OK, True, NOW + timedelta(minutes=2)) == "recovered"
    assert wd.step(LIVE_OK, True, NOW + timedelta(minutes=3)) is None
    assert [k for k, _ in sent] == ["down", "recovered"]


def test_quiet_feed_alerts_and_state_change_down_to_quiet():
    sent, notify = collector()
    wd = FeedWatchdog(notify)
    assert wd.step(LIVE_QUIET, True, NOW) == "quiet"
    # state change quiet -> down alerts immediately (no re-alert wait)
    assert wd.step(LIVE_DOWN, True, NOW + timedelta(minutes=1)) == "down"


def test_off_hours_and_synthetic_never_alert():
    sent, notify = collector()
    wd = FeedWatchdog(notify)
    assert wd.step(LIVE_DOWN, False, NOW) is None                     # closed
    assert wd.step({"mode": "synthetic", "connected": True}, True, NOW) is None
    assert wd.step({"mode": "off", "connected": False}, True, NOW) is None
    assert sent == []
    # and a broken state resets when the market closes (no stale re-alert at open)
    wd2 = FeedWatchdog(notify)
    wd2.step(LIVE_DOWN, True, NOW)
    wd2.step(LIVE_DOWN, False, NOW + timedelta(hours=5))
    assert wd2.state == "ok"


def test_session_windows_and_open_grace():
    wed = datetime(2026, 7, 15, 0, 0)
    def at(h, m):
        return wed.replace(hour=h, minute=m)
    # NSE: 09:15 open + 5min grace -> watchdog arms at 09:20
    assert not session_open_for({"NSE"}, at(9, 18))
    assert session_open_for({"NSE"}, at(9, 21))
    assert session_open_for({"NSE"}, at(15, 30))
    assert not session_open_for({"NSE"}, at(15, 31))
    # MCX evening session
    assert session_open_for({"MCX"}, at(22, 0))
    assert not session_open_for({"NSE"}, at(22, 0))
    # weekend: never
    sat = datetime(2026, 7, 18, 11, 0)
    assert not session_open_for({"NSE", "MCX"}, sat)
    # no subscribed segments -> nothing to police
    assert not session_open_for(set(), at(11, 0))
