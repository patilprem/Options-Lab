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


def _parse_ts(last_ts) -> Optional[datetime]:
    if not last_ts:
        return None
    try:
        return datetime.fromisoformat(str(last_ts))
    except (ValueError, TypeError):
        return None


def _age_s(last_ts: Optional[str], now: datetime) -> Optional[float]:
    """Seconds since a recording_health() last_ts string. None if never
    written or unparseable — 'never written today' is handled by the caller,
    since a table with no history at all is a different thing from a stalled
    one."""
    ts = _parse_ts(last_ts)
    return None if ts is None else (now - ts).total_seconds()


def recorded_underlyings() -> list:
    """The names the live recorder is responsible for, from settings.

    NOT pure — reads registry settings. Lives here (rather than in main.py,
    which is where it started) so the recorder and the watchdog that judges it
    can never disagree about what was supposed to be recording."""
    from app.core import registry
    rec = [u.strip() for u in registry.setting(
        "record_underlyings", "NIFTY,BANKNIFTY").split(",") if u.strip()]
    mcx = [u.strip() for u in registry.setting(
        "mcx_underlyings", "CRUDEOIL,GOLD").split(",") if u.strip()]
    return sorted(set(rec) | set(mcx))


def segment_for(underlying: str) -> str:
    """Which exchange session governs `underlying` — the same MCX_DYNAMIC test
    the recorder's own session gate uses, so a commodity is never judged
    against NSE hours (it records until 23:30, five hours past the NSE close)."""
    try:
        from app.data.dhan_client import MCX_DYNAMIC
    except Exception:
        return "NSE"
    return "MCX" if underlying in MCX_DYNAMIC else "NSE"


# Only underlying_bars gets an adaptive threshold, and the distinction is real:
# chain_snapshots and option_bars are written by the RECORDER on its own ~5-min
# cycle whenever the chain cache moves, which quotes do even when the contract
# barely trades — those are genuinely periodic, and loosening them would blunt
# the detector that has caught seven chain outages. underlying_bars is the odd
# one out: a row lands only when a 5-min candle COMPLETES, and that needs a tick
# in the next bucket, so a thin contract is event-driven in disguise.
SPARSE_TABLES = ("underlying_bars",)

# How many of a name's own typical write-intervals to tolerate before calling it
# stale. 3 absorbs an ordinary quiet stretch without letting a dead one hide.
SPARSE_MULTIPLE = 3.0


def _limit_s(row: dict, segment: str, stale_after_s: float) -> float:
    """Staleness threshold for one (table, underlying), never below
    `stale_after_s`.

    Pure. Infers the name's own write cadence from its row count and the time
    it spent writing — no per-instrument configuration to go stale when
    liquidity changes or a contract rolls.

    Cadence is measured up to the LAST WRITE, not up to `now`. Measuring to now
    would divide by the outage itself: a name that died at 09:30 has few rows,
    which would read as 'writes rarely' and RELAX the threshold exactly when it
    should tighten. Measuring to the last write asks 'how fast was it going
    while it was alive?', which is the question that matters."""
    from app.engines.watchdog import session_elapsed_s

    if row.get("table") not in SPARSE_TABLES:
        return stale_after_s
    n = row.get("rows_today") or 0
    last = _parse_ts(row.get("last_ts"))
    if n < 2 or last is None:
        return stale_after_s          # too little history to infer a cadence
    elapsed = session_elapsed_s(segment, last)
    if elapsed <= 0:
        return stale_after_s
    return max(stale_after_s, SPARSE_MULTIPLE * (elapsed / n))


def stale_underlyings(rows: list, now: datetime, segments: dict,
                      fresh_tables: set,
                      stale_after_s: float = STALE_AFTER_S,
                      grace_min: int = GRACE_MIN) -> list:
    """'table[UNDERLYING]' labels for names that should be recording but aren't.

    Pure. `rows` is store.recording_underlying_health() output, `segments` maps
    underlying -> "NSE"/"MCX", and `fresh_tables` is the set of tables that
    PASSED the table-level check.

    Only tables in `fresh_tables` are examined, and that is the whole point: if
    a table is already stale table-wide, stale_tables() has said so and naming
    every underlying inside it as well would just make the push longer without
    adding information. What this catches is the opposite and previously
    invisible case — the table looks alive because SOMETHING is writing to it,
    while the specific names we care about are dead (2026-07-31: chain_snapshots
    fresh from stock deep-dives, all four core underlyings frozen 90 minutes).

    A name that has NEVER been written (last_ts=None) is stale as soon as its
    session is open past the grace period.

    THRESHOLDS ADAPT TO THE INSTRUMENT. A flat 15 minutes assumes every name
    writes on a regular beat, and underlying_bars does not: a row only lands
    when a 5-min candle COMPLETES, and a candle only completes when a tick
    arrives in the next bucket. Big GOLD is thin — on 2026-08-03 it tripped
    this twice in an hour and recovered in 14 and 3 minutes, which is what a
    quiet contract looks like, not an outage (CRUDEOIL, same feed and same MCX
    session but far more liquid, was never flagged). Judging it on a clock it
    was never going to meet is how an alarm becomes noise, and this codebase
    has already paid five days for an alarm nobody trusted.

    So a name that has been writing every ~N seconds today is given
    SPARSE_MULTIPLE x N before it counts as stale, never less than
    `stale_after_s`. A genuinely dead sparse name still trips — its age keeps
    growing past any threshold — just later, which for a 5-minute recorder is
    the right trade."""
    out = []
    for row in rows:
        table, u = row.get("table"), row.get("underlying")
        if not u or table not in fresh_tables or not row.get("present", True):
            continue
        seg = segments.get(u) or "NSE"
        if not session_open_for((seg,), now, grace_min):
            continue
        age = _age_s(row.get("last_ts"), now)
        if age is None or age > _limit_s(row, seg, stale_after_s):
            out.append(f"{table}[{u}]")
    return sorted(out)


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

    def step(self, health: list, now: datetime,
             underlying_rows: Optional[list] = None,
             segments: Optional[dict] = None) -> Optional[str]:
        """Evaluate one sample. Returns the push kind sent ('stale' |
        'recovered') or None for silence.

        `underlying_rows` (store.recording_underlying_health()) is optional so
        a caller with only table-level health — or a test — behaves exactly as
        before. When supplied, per-underlying staleness is checked for the
        tables that passed the table-level test, which is where a stalled core
        underlying hides behind a table another writer is keeping warm."""
        tables = stale_tables(health, now, self.stale_after_s)
        cur = list(tables)
        if underlying_rows:
            stale_set = set(tables)
            fresh = {r.get("table") for r in health
                     if r.get("table") not in stale_set}
            cur += stale_underlyings(underlying_rows, now, segments or {},
                                     fresh, self.stale_after_s)
        cur = tuple(cur)

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
