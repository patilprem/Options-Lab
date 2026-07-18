"""
Feed watchdog — taps your shoulder (ntfy push) when the market-data feed is
down or silent DURING MARKET HOURS, and sends an all-clear when it recovers.

Why: the 2026-07-13 frozen-chain incident was invisible until fills came out
wrong. The dashboard's Feed pill shows problems if you're looking; this
pushes when you're not.

Design: FeedWatchdog.step() is a pure decision function (fully offline-
testable); MarketHub calls it once a minute with feed_status() + the clock.
Alert etiquette: one push on state change, re-push every REALERT_MIN while
still broken, one recovery push, silence otherwise. Off-hours and synthetic
mode never alert.
"""

from __future__ import annotations

import os
from datetime import datetime, time as dtime, timedelta
from typing import Callable, Iterable, Optional

QUIET_AFTER_S = 180        # connected but no tick for this long = "quiet"
REALERT_MIN = 15           # minutes between repeat pushes while still broken
GRACE_MIN = 5              # ignore the first minutes after open (slow first tick)

NSE_SESSION = (dtime(9, 15), dtime(15, 30))
MCX_SESSION = (dtime(9, 0), dtime(23, 30))


def feed_broken(status: dict, market_open: bool) -> bool:
    """True when the live feed needs recovery DURING market hours: either the
    socket is down, or it is connected but silent (no tick for QUIET_AFTER_S —
    e.g. subscribed to a dead/rolled MCX contract, or a stale connection that
    LiveFeed's dropped-socket reconnect can't detect). Off-hours / synthetic
    are never 'broken'. Pure — mirrors FeedWatchdog.step's health test so the
    self-heal action and the alert stay in lockstep."""
    if not market_open or status.get("mode") != "live":
        return False
    if not status.get("connected"):
        return True
    age = status.get("tick_age_sec")
    return age is None or age > QUIET_AFTER_S


def session_open_for(segments: Iterable[str], now: datetime,
                     grace_min: int = GRACE_MIN) -> bool:
    """True if any watched segment's session is open (Mon-Fri, IST wall clock),
    with a grace period after the open so the first tick has time to arrive."""
    if now.weekday() >= 5:
        return False
    for seg in segments:
        start, end = MCX_SESSION if seg == "MCX" else NSE_SESSION
        start_dt = datetime.combine(now.date(), start) + timedelta(minutes=grace_min)
        end_dt = datetime.combine(now.date(), end)
        if start_dt <= now <= end_dt:
            return True
    return False


def push_ntfy(message: str, kind: str) -> bool:
    """Same channel as the token-refresh link (NTFY_TOPIC env)."""
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic:
        print(f"[watchdog] (set NTFY_TOPIC for phone push) {message}")
        return False
    try:
        import requests
        requests.post(
            f"https://ntfy.sh/{topic}", data=f"OptionsLab: {message}",
            headers={"Title": "OptionsLab feed watchdog",
                     "Priority": "urgent" if kind == "down" else "high",
                     "Tags": "white_check_mark" if kind == "recovered" else "warning"},
            timeout=10)
        return True
    except Exception as e:
        print(f"[watchdog] ntfy push failed: {e}")
        return False


class FeedWatchdog:
    def __init__(self, notify: Optional[Callable[[str, str], bool]] = None):
        self.notify = notify or push_ntfy
        self.state = "ok"                       # ok | down | quiet
        self._last_alert: Optional[datetime] = None

    def step(self, status: dict, market_open: bool, now: datetime) -> Optional[str]:
        """Evaluate one health sample. Returns the push kind sent this step
        ('down' | 'quiet' | 'recovered') or None for silence."""
        if not market_open or status.get("mode") != "live":
            # off-hours / synthetic: reset quietly, never alert
            self.state, self._last_alert = "ok", None
            return None

        if not status.get("connected"):
            cur = "down"
        else:
            age = status.get("tick_age_sec")
            cur = "quiet" if (age is None or age > QUIET_AFTER_S) else "ok"

        if cur == "ok":
            if self.state != "ok":
                self.state, self._last_alert = "ok", None
                self.notify("feed RECOVERED — ticks flowing again", "recovered")
                return "recovered"
            return None

        due = (self._last_alert is None
               or (now - self._last_alert).total_seconds() >= REALERT_MIN * 60)
        if cur != self.state or due:
            self.state, self._last_alert = cur, now
            msg = ("market-data feed DISCONNECTED during market hours"
                   if cur == "down" else
                   f"feed connected but SILENT >{QUIET_AFTER_S}s during market hours")
            self.notify(msg, cur)
            return cur
        return None
