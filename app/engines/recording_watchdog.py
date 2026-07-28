"""Recording watchdog — pushes when a RECORDER stops writing, feed or no feed.

Why this exists separately from watchdog.py: the 2026-07-23..27 outage never
touched the feed. Ticks flowed the whole time, underlying_bars kept writing its
150 rows a day, the dashboard's feed pill stayed green — and chain_snapshots
recorded NOTHING for the index/commodity names for five days. FeedWatchdog
watches the socket, so it had nothing to say. `store.recording_health()` knew,
but only answered when someone curled /data/health, and nobody did for five
days. The MCX half of what was lost can never be re-fetched.

So this watches the OUTPUT (are rows landing?) rather than the transport, and
pushes to the phone instead of waiting to be asked.

Design mirrors FeedWatchdog deliberately — a pure `step()` over a
recording_health() sample plus the clock, fully offline-testable, with the
same alert etiquette: one push when a table goes stale, a re-push every
REALERT_MIN while it stays stale, one all-clear on recovery, silence
otherwise. An alarm that fires constantly is one you learn to ignore, which is
how the five days happened in the first place.

Two rules keep it quiet when it should be:

  * SEGMENT-AWARE. A table is only judged while a session that FEEDS it is
    open. stock_snapshots stops at the NSE close by design; judging it against
    "any session open" flagged it for the whole 8-hour MCX evening, every
    night — /data/health made exactly that mistake on its first evening.
  * EVENT-DRIVEN TABLES ARE NEVER FLAGGED. setup_flags only writes when a
    setup clears its threshold, so a quiet stretch is normal and age says
    nothing about health. `periodic=False` in _RECORDED_TABLES marks those.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from app.engines.watchdog import GRACE_MIN, REALERT_MIN, session_open_for

# A periodic recorder that has written nothing for this long, while a session
# feeding it is open, is broken. Generous on purpose: the chain poller is rate
# limited to 1 request per 3s across many underlyings, and a slow sweep must
# not read as an outage.
STALE_AFTER_S = 900          # 15 minutes


def _age_s(last_ts: Optional[str], now: datetime) -> Optional[float]:
    """Seconds since a recording_health() last_ts string. None if never
    written or unparseable — 'never written today' is handled by the caller,
    since a table with no history at all is a different thing from a stalled
    one."""
    if not last_ts:
        return None
    try:
        return (now - datetime.fromisoformat(str(last_ts))).total_seconds()
    except (ValueError, TypeError):
        return None


def stale_tables(health: list, now: datetime,
                 stale_after_s: float = STALE_AFTER_S,
                 grace_min: int = GRACE_MIN) -> list:
    """Names of tables that SHOULD be receiving rows right now but aren't.

    Pure. `health` is store.recording_health() output. A table qualifies only
    when it is periodic, present, one of its feeding segments is open (past
    the post-open grace period), and its newest row is older than
    `stale_after_s`.
    """
    out = []
    for row in health:
        if not row.get("periodic", True) or not row.get("present", True):
            continue
        if not session_open_for(row.get("segments") or (), now, grace_min):
            continue
        age = _age_s(row.get("last_ts"), now)
        if age is None or age > stale_after_s:
            out.append(row["table"])
    return sorted(out)


class RecordingWatchdog:
    """Turns a stream of recording_health() samples into phone pushes."""

    def __init__(self, notify: Optional[Callable[[str, str], bool]] = None,
                 stale_after_s: float = STALE_AFTER_S):
        from app.engines.watchdog import push_ntfy
        self.notify = notify or push_ntfy
        self.stale_after_s = stale_after_s
        self.stale: tuple = ()                  # last reported stale set
        self._last_alert: Optional[datetime] = None

    def step(self, health: list, now: datetime) -> Optional[str]:
        """Evaluate one sample. Returns the push kind sent ('stale' |
        'recovered') or None for silence."""
        cur = tuple(stale_tables(health, now, self.stale_after_s))

        if not cur:
            if self.stale:
                self.stale, self._last_alert = (), None
                self.notify("recording RECOVERED — all recorders writing again",
                            "recovered")
                return "recovered"
            return None

        # Re-push on a CHANGED set as well as on the timer: a second table
        # going dark while the first is still down is new information, and
        # waiting out the re-alert window would hide it.
        due = (self._last_alert is None
               or (now - self._last_alert).total_seconds() >= REALERT_MIN * 60)
        if cur != self.stale or due:
            self.stale, self._last_alert = cur, now
            mins = int(self.stale_after_s // 60)
            self.notify(
                f"NOT RECORDING for >{mins}min during market hours: "
                f"{', '.join(cur)}", "stale")
            return "stale"
        return None
