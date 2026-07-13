"""
Backfill job runner with SQLite-persisted status
================================================
Wraps dhan_client.backfill and records live progress to registry
(`backfill_status`) so the Data tab shows what's running — whether it was
started by the in-app /data/backfill endpoint or a standalone background job:

    python -m app.data.backfill_job NIFTY 2024-07-10 2025-12-31

SQLite is multi-process safe, so a subprocess job and the running server both
see the same status.
"""

from __future__ import annotations

import time
from datetime import date

from app.core import registry
from app.data import dhan_client


def _chunks(days: int, size: int) -> int:
    return max(1, -(-days // size))  # ceil


STALE_SEC = 180


def another_run_active() -> bool:
    """A backfill is already alive if its heartbeat is fresh. Prevents two
    concurrent jobs from racing (which previously produced silent data gaps)."""
    st = registry.get_backfill_status()
    return bool(st.get("running") and st.get("updated_at")
                and time.time() - st["updated_at"] < STALE_SEC)


def run(underlying: str, start: date, end: date, *, strike_offsets: int = 2,
        interval: int = 5, store=None, force: bool = False) -> dict:
    if not force and another_run_active():
        msg = "another backfill is already running (fresh heartbeat); refusing to start"
        registry.record_event("warn", "data", msg)
        raise RuntimeError(msg)
    offs = range(-strike_offsets, strike_offsets + 1)
    days = (end - start).days + 1
    state = {
        "running": True, "underlying": underlying, "done": 0,
        "total": _chunks(days, 89) + len(offs) * 2 * _chunks(days, 29),
        "from": start.isoformat(), "to": end.isoformat(),
        "message": "starting…", "error": None, "updated_at": time.time(),
    }
    registry.set_backfill_status(state)
    registry.record_event("info", "data", f"Backfill {underlying} {start}→{end} started")

    def progress(msg: str):
        state["done"] += 1
        state["message"] = msg
        state["updated_at"] = time.time()      # heartbeat: a dead job goes stale
        registry.set_backfill_status(state)

    try:
        dhan_client.backfill(underlying, start, end, strike_offsets=offs,
                             interval=interval, store=store, progress=progress)
        # The rolling expired-options API returns no expiry date; derive the
        # calendar from the data (ATM-straddle expiry-day collapse) and fill
        # the NULL expiry column so contracts are identifiable across weeks.
        if store is not None and hasattr(store, "_q"):
            from app.data import expiries
            try:
                res = expiries.rebuild(store, underlying)
                registry.record_event(
                    "info", "data",
                    f"expiry calendar: {res['expiries']} expiries, "
                    f"{res['rows_filled']} rows filled")
            except Exception as e:      # calendar is best-effort, never fatal
                registry.record_event("warn", "data", f"expiry rebuild: {e!r}")
        state["message"] = "done"
        registry.record_event("info", "data",
                              f"Backfill {underlying} complete ({state['done']} chunks)")
    except Exception as e:
        state["error"] = repr(e)
        state["message"] = "failed"
        registry.record_event("error", "data", f"Backfill failed: {e!r}")
    finally:
        state["running"] = False
        state["updated_at"] = time.time()
        registry.set_backfill_status(state)
    return state


if __name__ == "__main__":
    import sys
    _, und, s, e = sys.argv[:4]
    offs = int(sys.argv[4]) if len(sys.argv) > 4 else 2
    registry.init_db()
    run(und, date.fromisoformat(s), date.fromisoformat(e), strike_offsets=offs)
