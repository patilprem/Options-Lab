"""Recording watchdog: would it have fired on 2026-07-23?

That is the test this module exists to pass. During the 07-23..27 outage the
feed was healthy the whole time — ticks flowing, underlying_bars writing, the
dashboard pill green — while chain_snapshots recorded nothing for the index
names for five days. FeedWatchdog watches the socket and had nothing to say.

The second thing under test is SILENCE. An alarm that fires every evening
because an NSE table is idle during the MCX session is one you learn to
ignore, which is how five days went unnoticed. Roughly half these tests assert
that nothing is pushed.
"""

from datetime import datetime

from app.engines.recording_watchdog import (RecordingWatchdog, stale_tables,
                                            STALE_AFTER_S)

# 2026-07-28 is a Tuesday.
NSE_MID = datetime(2026, 7, 28, 11, 0)     # NSE + MCX both open
MCX_EVENING = datetime(2026, 7, 28, 19, 0)  # NSE closed, MCX open
CLOSED = datetime(2026, 7, 28, 2, 0)        # nothing open
WEEKEND = datetime(2026, 8, 1, 11, 0)       # Saturday


def _t(table, last_ts, segments, periodic=True, present=True):
    return {"table": table, "last_ts": last_ts.isoformat() if last_ts else None,
            "segments": list(segments), "periodic": periodic, "present": present,
            "rows_today": 1, "sessions": 1, "error": None}


def _fresh(now, secs=60):
    from datetime import timedelta
    return now - timedelta(seconds=secs)


def _stale(now, secs=STALE_AFTER_S + 300):
    from datetime import timedelta
    return now - timedelta(seconds=secs)


# --- the incident ----------------------------------------------------------

def test_would_have_fired_on_the_chain_outage():
    """THE test. Feed healthy, underlying_bars writing, chain_snapshots dead."""
    health = [_t("underlying_bars", _fresh(NSE_MID), ("NSE", "MCX")),
              _t("chain_snapshots", _stale(NSE_MID), ("NSE", "MCX")),
              _t("stock_snapshots", _fresh(NSE_MID), ("NSE",))]
    assert stale_tables(health, NSE_MID) == ["chain_snapshots"]

    pushed = []
    wd = RecordingWatchdog(notify=lambda m, k: pushed.append((m, k)))
    assert wd.step(health, NSE_MID) == "stale"
    assert "chain_snapshots" in pushed[0][0]
    assert "NOT RECORDING" in pushed[0][0]


def test_a_table_that_never_wrote_today_is_stale_not_ignored():
    """last_ts=None must not read as 'fine' — that IS the outage's signature."""
    health = [_t("chain_snapshots", None, ("NSE", "MCX"))]
    assert stale_tables(health, NSE_MID) == ["chain_snapshots"]


# --- silence ---------------------------------------------------------------

def test_everything_fresh_is_silent():
    health = [_t("underlying_bars", _fresh(NSE_MID), ("NSE", "MCX")),
              _t("chain_snapshots", _fresh(NSE_MID), ("NSE", "MCX"))]
    wd = RecordingWatchdog(notify=lambda m, k: (_ for _ in ()).throw(
        AssertionError("must not push")))
    assert wd.step(health, NSE_MID) is None


def test_nse_table_idle_during_mcx_evening_is_not_flagged():
    """The /data/health first-evening mistake: stock_snapshots correctly stops
    at the NSE close, and flagging it for 8 hours nightly trains you to ignore
    the alarm."""
    health = [_t("stock_snapshots", _stale(MCX_EVENING), ("NSE",)),
              _t("chain_snapshots", _fresh(MCX_EVENING), ("NSE", "MCX"))]
    assert stale_tables(health, MCX_EVENING) == []


def test_mcx_table_stale_during_mcx_evening_is_flagged():
    """The converse must still work, or the segment rule is just a mute."""
    health = [_t("chain_snapshots", _stale(MCX_EVENING), ("NSE", "MCX"))]
    assert stale_tables(health, MCX_EVENING) == ["chain_snapshots"]


def test_nothing_is_flagged_outside_market_hours():
    health = [_t("chain_snapshots", _stale(CLOSED), ("NSE", "MCX"))]
    assert stale_tables(health, CLOSED) == []


def test_nothing_is_flagged_at_the_weekend():
    health = [_t("chain_snapshots", None, ("NSE", "MCX"))]
    assert stale_tables(health, WEEKEND) == []


def test_event_driven_tables_are_never_flagged():
    """setup_flags only writes when a setup clears its threshold; a quiet
    stretch says nothing about health."""
    health = [_t("setup_flags", _stale(NSE_MID), ("NSE",), periodic=False)]
    assert stale_tables(health, NSE_MID) == []


def test_missing_table_is_not_flagged():
    health = [_t("chain_snapshots", None, ("NSE",), present=False)]
    assert stale_tables(health, NSE_MID) == []


def test_grace_period_after_the_open():
    """No table has written at 09:16; that's the open, not an outage."""
    just_open = datetime(2026, 7, 28, 9, 16)
    health = [_t("stock_snapshots", None, ("NSE",))]
    assert stale_tables(health, just_open) == []


# --- alert etiquette -------------------------------------------------------

def test_one_push_per_state_change_not_one_per_minute():
    from datetime import timedelta
    pushed = []
    wd = RecordingWatchdog(notify=lambda m, k: pushed.append(k))
    health = [_t("chain_snapshots", _stale(NSE_MID), ("NSE", "MCX"))]
    assert wd.step(health, NSE_MID) == "stale"
    for i in range(1, 10):                       # nine more minutes, same state
        assert wd.step(health, NSE_MID + timedelta(minutes=i)) is None
    assert pushed == ["stale"]


def test_repushes_after_the_realert_window():
    from datetime import timedelta
    from app.engines.watchdog import REALERT_MIN
    pushed = []
    wd = RecordingWatchdog(notify=lambda m, k: pushed.append(k))
    health = [_t("chain_snapshots", _stale(NSE_MID), ("NSE", "MCX"))]
    wd.step(health, NSE_MID)
    later = NSE_MID + timedelta(minutes=REALERT_MIN + 1)
    assert wd.step(health, later) == "stale"
    assert pushed == ["stale", "stale"]


def test_a_second_table_going_dark_pushes_immediately():
    """New information must not wait out the re-alert timer."""
    from datetime import timedelta
    pushed = []
    wd = RecordingWatchdog(notify=lambda m, k: pushed.append(m))
    wd.step([_t("chain_snapshots", _stale(NSE_MID), ("NSE", "MCX"))], NSE_MID)
    worse = [_t("chain_snapshots", _stale(NSE_MID), ("NSE", "MCX")),
             _t("option_bars", _stale(NSE_MID), ("NSE", "MCX"))]
    assert wd.step(worse, NSE_MID + timedelta(minutes=1)) == "stale"
    assert "option_bars" in pushed[-1]


def test_recovery_pushes_once_then_goes_quiet():
    from datetime import timedelta
    pushed = []
    wd = RecordingWatchdog(notify=lambda m, k: pushed.append(k))
    wd.step([_t("chain_snapshots", _stale(NSE_MID), ("NSE", "MCX"))], NSE_MID)
    ok = [_t("chain_snapshots", _fresh(NSE_MID), ("NSE", "MCX"))]
    t1 = NSE_MID + timedelta(minutes=1)
    assert wd.step(ok, t1) == "recovered"
    assert wd.step(ok, t1 + timedelta(minutes=1)) is None
    assert pushed == ["stale", "recovered"]


def test_never_alerted_means_no_spurious_recovery():
    """A clean start must not announce a recovery from nothing."""
    pushed = []
    wd = RecordingWatchdog(notify=lambda m, k: pushed.append(k))
    assert wd.step([_t("chain_snapshots", _fresh(NSE_MID), ("NSE", "MCX"))],
                   NSE_MID) is None
    assert pushed == []


def test_empty_health_is_silent():
    wd = RecordingWatchdog(notify=lambda m, k: None)
    assert wd.step([], NSE_MID) is None
