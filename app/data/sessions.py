"""Canonical exchange trading windows.

THE single place a timestamp is judged in-session or not. Every write into
`underlying_bars` must pass through `in_session()` — see the incident note
below for why a tick-only gate was not enough.

Incident (2026-07-27). Two independent bugs, both about session windows:

  1. `MarketHub._tick_ok` hardcoded NSE's 09:15-15:30 for EVERY underlying,
     so every MCX tick after 15:30 was discarded. MCX trades to 23:30, so
     underlying_bars held no MCX history at all.
  2. Post-close phantom candles appeared for NSE names — NIFTY at 16:10 and
     BANKNIFTY at 16:05, both flat (O==H==L==C) with zero volume, the
     signature of a single stale snapshot price rather than a real bar.
     They got in because the tick gate guards TICKS, and nothing guarded
     WRITES: the nightly gap-repair backfill inserts whatever Dhan returns
     straight into the table.

A flat zero-volume bar is not harmless. It is indistinguishable from a real
candle to `store.underlying_bars()`, so indicator warmup, VWAP, and any
backtest replaying that day consume it as truth.

Windows are deliberately the REGULAR session only. Special sessions (NSE's
Muhurat evening trade) and holidays/modified sessions (a full closure, or
MCX opening late) are handled as dated exceptions in SESSION_OVERRIDES below
so both the tick path and the write path learn about them at once — and so
does every watchdog, via session_window()/watchdog.session_open_for().
"""

from __future__ import annotations

from datetime import date as ddate, time as dtime

# Regular-session windows, inclusive at both ends. Bucket-start labelling
# means a 15:30 bar is the last NSE bar of the day.
SESSION_WINDOW: dict[str, tuple[dtime, dtime]] = {
    "MCX": (dtime(9, 0), dtime(23, 30)),
    "NSE": (dtime(9, 15), dtime(15, 30)),
}

# Unknown underlyings get the STRICTER window: letting junk in is worse than
# dropping a bar for a name nobody configured.
DEFAULT_SEGMENT = "NSE"

# Dated exceptions to the regular weekly window: exchange holidays (segment
# doesn't trade at all -> None) and modified/special sessions (a one-off
# window that isn't the usual open/close), keyed by (segment, "YYYY-MM-DD").
# There is no holiday-calendar API (see CLAUDE.md) so this is maintained by
# hand, the same dated-table pattern as backtest.py's LOT_HISTORY. Every
# session-window consumer (in_session() here, and watchdog.session_open_for /
# session_elapsed_s) must read through session_window() so an entry added
# here is honoured everywhere at once — a duplicate hardcoded window is
# exactly the class of bug the 2026-07-27 incident was about.
# 2026 calendar: NSE's official holiday-master (the FO list for NSE/BSE, the
# COM list for MCX's morning/evening sessions), fetched 2026-10-02. Rules it
# encodes: NSE/BSE close all day on a holiday; MCX either closes both sessions
# (None), loses only the morning session (window starts 17:00), or — New Year's
# Day — loses only the evening (window ends at the 17:00 session break; the
# close is 16:59:59 because the 17:00 bar belongs to the evening session).
# Weekend holidays (2026-02-15, 03-21, 08-15, 11-08) are omitted: in_session()
# rejects weekends before it ever consults this table. 2026-11-08 (Sun) is
# Diwali Laxmi Pujan, where NSE/MCX hold a Muhurat evening session whose
# timings are announced late — add a dated entry here once published, and lift
# the weekend check for it. Refresh this table each December from
# https://www.nseindia.com/api/holiday-master?type=trading (needs a browser
# User-Agent and a nseindia.com Referer); MCX's own page blocks scripts.
SESSION_OVERRIDES: dict[tuple[str, str], tuple[dtime, dtime] | None] = {
    # 2026-01-01 (Thu): New year
    # (NSE/BSE trade normally)
    ("MCX", "2026-01-01"): (dtime(9, 0), dtime(16, 59, 59)),
    # 2026-01-15 (Thu): Municipal Corporation Election - Maharashtra
    ("NSE", "2026-01-15"): None,
    ("MCX", "2026-01-15"): (dtime(17, 0), dtime(23, 30)),
    # 2026-01-26 (Mon): Republic Day
    ("NSE", "2026-01-26"): None,
    ("MCX", "2026-01-26"): None,
    # 2026-03-03 (Tue): Holi
    ("NSE", "2026-03-03"): None,
    ("MCX", "2026-03-03"): (dtime(17, 0), dtime(23, 30)),
    # 2026-03-26 (Thu): Shri Ram Navami
    ("NSE", "2026-03-26"): None,
    ("MCX", "2026-03-26"): (dtime(17, 0), dtime(23, 30)),
    # 2026-03-31 (Tue): Shri Mahavir Jayanti
    ("NSE", "2026-03-31"): None,
    ("MCX", "2026-03-31"): (dtime(17, 0), dtime(23, 30)),
    # 2026-04-03 (Fri): Good Friday
    ("NSE", "2026-04-03"): None,
    ("MCX", "2026-04-03"): None,
    # 2026-04-14 (Tue): Dr. Baba Saheb Ambedkar Jayanti
    ("NSE", "2026-04-14"): None,
    ("MCX", "2026-04-14"): (dtime(17, 0), dtime(23, 30)),
    # 2026-05-01 (Fri): Maharashtra Day
    ("NSE", "2026-05-01"): None,
    ("MCX", "2026-05-01"): (dtime(17, 0), dtime(23, 30)),
    # 2026-05-28 (Thu): Bakri Id
    ("NSE", "2026-05-28"): None,
    ("MCX", "2026-05-28"): (dtime(17, 0), dtime(23, 30)),
    # 2026-06-26 (Fri): Muharram
    ("NSE", "2026-06-26"): None,
    ("MCX", "2026-06-26"): (dtime(17, 0), dtime(23, 30)),
    # 2026-09-14 (Mon): Ganesh Chaturthi
    ("NSE", "2026-09-14"): None,
    ("MCX", "2026-09-14"): (dtime(17, 0), dtime(23, 30)),
    # 2026-10-02 (Fri): Mahatma Gandhi Jayanti — MCX's morning AND evening
    # sessions both closed. Missing this made the feed/recording watchdogs push
    # "NOT RECEIVING" to ntfy all day on a holiday.
    ("NSE", "2026-10-02"): None,
    ("MCX", "2026-10-02"): None,
    # 2026-10-20 (Tue): Dussehra
    ("NSE", "2026-10-20"): None,
    ("MCX", "2026-10-20"): (dtime(17, 0), dtime(23, 30)),
    # 2026-11-10 (Tue): Diwali-Balipratipada
    ("NSE", "2026-11-10"): None,
    ("MCX", "2026-11-10"): (dtime(17, 0), dtime(23, 30)),
    # 2026-11-24 (Tue): Prakash Gurpurb Sri Guru Nanak Dev
    ("NSE", "2026-11-24"): None,
    ("MCX", "2026-11-24"): (dtime(17, 0), dtime(23, 30)),
    # 2026-12-25 (Fri): Christmas
    ("NSE", "2026-12-25"): None,
    ("MCX", "2026-12-25"): None,
}


def session_window(seg: str, on_date: ddate) -> tuple[dtime, dtime] | None:
    """The (open, close) window for `seg` on `on_date`, or None if the
    segment doesn't trade at all that day. Checks SESSION_OVERRIDES first,
    else falls back to the regular window. Callers still own the Mon-Fri
    weekend check."""
    override_key = (seg, on_date.isoformat())
    if override_key in SESSION_OVERRIDES:
        return SESSION_OVERRIDES[override_key]
    return SESSION_WINDOW.get(seg, SESSION_WINDOW[DEFAULT_SEGMENT])


def segment_for(underlying: str) -> str:
    """Exchange bucket for `underlying`, from dhan_client.UNDERLYINGS.

    Imported lazily: dhan_client's MCX entries are populated at runtime by
    the dynamic resolver, and a module-level import here would be circular.
    """
    try:
        from app.data.dhan_client import UNDERLYINGS
    except Exception:                       # pragma: no cover - import guard
        return DEFAULT_SEGMENT
    cfg = UNDERLYINGS.get(underlying) or {}
    return "MCX" if "MCX" in str(cfg.get("segment", "")) else DEFAULT_SEGMENT


def in_session(ts, underlying: str = "", segment: str | None = None) -> bool:
    """True if `ts` (naive IST) falls inside its exchange's regular session.

    Weekends are always out. Pass `segment` to skip the UNDERLYINGS lookup
    (tests, and hot paths that already resolved it).
    """
    if ts.weekday() >= 5:
        return False
    seg = segment or segment_for(underlying)
    window = session_window(seg, ts.date())
    if window is None:
        return False
    lo, hi = window
    return lo <= ts.time() <= hi
