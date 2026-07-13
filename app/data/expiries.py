"""
Expiry calendar — derived from the data, not hardcoded
======================================================
Dhan's rolling expired-options API returns no expiry date, so backfilled
option_bars rows have expiry NULL. Hardcoding a weekday rule is unsafe: the
NIFTY weekly expiry moved Thursday→Tuesday in Sep-2025 and individual weeks
shift for holidays (observed Wed/Mon expiries around Independence Day, Diwali,
etc.).

Instead we DERIVE the calendar from the recorded series: on expiry day the
ATM (offset-0) straddle premium collapses to ~intrinsic by the close — a deep
local minimum vs the trailing days. Validated on 2y NIFTY: 102 weekly expiries
detected, matching the known Thursday→Tuesday transition and holiday shifts.

`rebuild(store, underlying)` persists the detected dates into expiry_calendar
and fills the NULL expiry column (offset-aware: WEEKLY k = the (k+1)-th
detected expiry >= the bar's date). Live-recorded rows already carry a real
expiry and are never touched. Idempotent; run after every backfill.
"""

from __future__ import annotations

import statistics
from bisect import bisect_left
from datetime import date

CAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS expiry_calendar (
    underlying VARCHAR, expiry DATE,
    PRIMARY KEY (underlying, expiry)
);
"""


def detect_weekly_expiries(store, underlying: str,
                           collapse_ratio: float = 0.4,
                           min_median: float = 20.0) -> list[date]:
    """Expiry days = days whose ATM-straddle end-of-day premium is below
    `collapse_ratio` x the trailing-5-day median (and the median is a real
    premium, not noise)."""
    rows = store._q(
        """WITH lastbar AS (
             SELECT CAST(ts AS DATE) d, option_type, last(close ORDER BY ts) c
             FROM option_bars
             WHERE underlying=? AND strike_offset=0 AND expiry_kind='WEEKLY'
               AND expiry_offset=0
             GROUP BY 1, 2)
           SELECT d, sum(CASE WHEN option_type='CALL' THEN c END),
                     sum(CASE WHEN option_type='PUT' THEN c END)
           FROM lastbar GROUP BY d ORDER BY d""", [underlying])
    days = [(d, (ce or 0) + (pe or 0)) for d, ce, pe in rows]
    out = []
    for i, (d, v) in enumerate(days):
        window = [x for _, x in days[max(0, i - 5):i]] or [v]
        med = statistics.median(window)
        if med > min_median and v < collapse_ratio * med:
            out.append(d)
    return out


def rebuild(store, underlying: str) -> dict:
    """Re-derive the calendar and fill NULL expiries. Returns counters."""
    store.con.execute(CAL_SCHEMA)
    expiries = detect_weekly_expiries(store, underlying)
    if not expiries:
        return {"expiries": 0, "updated": 0}
    with store._lock:
        store.con.execute("DELETE FROM expiry_calendar WHERE underlying=?",
                          [underlying])
        store.con.executemany(
            "INSERT INTO expiry_calendar VALUES (?,?)",
            [(underlying, e) for e in expiries])

    # Fill NULL expiry, offset-aware: bar at date D with expiry_offset k gets
    # the (k+1)-th calendar expiry >= D. Bars beyond the last detected expiry
    # stay NULL (unknowable until more data arrives).
    offsets = [r[0] for r in store._q(
        """SELECT DISTINCT expiry_offset FROM option_bars
           WHERE underlying=? AND expiry_kind='WEEKLY' AND expiry IS NULL""",
        [underlying])]
    before = store._q1(
        "SELECT count(*) FROM option_bars WHERE underlying=? AND expiry IS NULL",
        [underlying])[0]
    for k in offsets:
        dates = [r[0] for r in store._q(
            """SELECT DISTINCT CAST(ts AS DATE) FROM option_bars
               WHERE underlying=? AND expiry_kind='WEEKLY'
                 AND expiry_offset=? AND expiry IS NULL""", [underlying, k])]
        pairs = [(expiries[i], d) for d in dates
                 if (i := bisect_left(expiries, d) + k) < len(expiries)]
        with store._lock:
            for exp, d in pairs:
                store.con.execute(
                    """UPDATE option_bars SET expiry=?
                       WHERE underlying=? AND expiry_kind='WEEKLY'
                         AND expiry_offset=? AND expiry IS NULL
                         AND CAST(ts AS DATE)=?""", [exp, underlying, k, d])
    after = store._q1(
        "SELECT count(*) FROM option_bars WHERE underlying=? AND expiry IS NULL",
        [underlying])[0]
    return {"expiries": len(expiries), "rows_filled": before - after,
            "rows_still_null": after}
