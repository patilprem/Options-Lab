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
Muhurat evening trade) are excluded; if one is ever needed, add it here so
both the tick path and the write path learn about it at once.
"""

from __future__ import annotations

from datetime import time as dtime

# Regular-session windows, inclusive at both ends. Bucket-start labelling
# means a 15:30 bar is the last NSE bar of the day.
SESSION_WINDOW: dict[str, tuple[dtime, dtime]] = {
    "MCX": (dtime(9, 0), dtime(23, 30)),
    "NSE": (dtime(9, 15), dtime(15, 30)),
}

# Unknown underlyings get the STRICTER window: letting junk in is worse than
# dropping a bar for a name nobody configured.
DEFAULT_SEGMENT = "NSE"


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
    lo, hi = SESSION_WINDOW.get(seg, SESSION_WINDOW[DEFAULT_SEGMENT])
    return lo <= ts.time() <= hi
