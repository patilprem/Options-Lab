#!/usr/bin/env python3
"""Did the options data see it coming? — event-window signal report (CLI).

Thin CLI over app/engines/event_signals.py (the SAME logic the
/data/event_window_signals API endpoint runs in-process). For each
underlying x event time, prints:

  1. Spot path through the window (from underlying_bars).
  2. A PCR / ATM-IV / IV-skew / max-pain timeline sampled every `--sample`
     minutes from `--before` minutes ahead of the event to `--after` minutes
     after it.
  3. The strikes with the biggest OI swing in the window (top --top-strikes
     by |delta|, split CALL/PUT) — the "where did positioning build" view.
  4. The index_bias reading (if recorded) just before vs at the event.

READ-ONLY — never touches the registry or the store. DuckDB only allows one
process to hold marketdata.duckdb open at a time (even for reads), so this
CLI can only see real data when the app process is NOT running. While the
app is live, use GET /data/event_window_signals instead (same report,
served from the app's already-open connection).

Usage:
    venv/bin/python -m scripts.event_window_signals
    venv/bin/python -m scripts.event_window_signals --date 2026-07-21 \\
        --underlying NIFTY BANKNIFTY --events 12:30 13:00 \\
        --before 20 --after 10 --sample 5 --top-strikes 8
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data.store import get_store                  # noqa: E402
from app.engines.event_signals import build_report     # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", type=date.fromisoformat, default=date.today(),
                    help="session date, YYYY-MM-DD (default: today, server-local)")
    ap.add_argument("--underlying", nargs="+", default=["NIFTY", "BANKNIFTY"])
    ap.add_argument("--events", nargs="+", default=["12:30", "13:00"],
                    help="event times, HH:MM (24h, IST)")
    ap.add_argument("--before", type=int, default=20, help="minutes before the event to start")
    ap.add_argument("--after", type=int, default=10, help="minutes after the event to end")
    ap.add_argument("--sample", type=int, default=5, help="minutes between metric samples")
    ap.add_argument("--top-strikes", type=int, default=8)
    ap.add_argument("--max-age-min", type=int, default=10,
                    help="reject a chain/bias reading older than this many minutes")
    args = ap.parse_args()

    store = get_store()
    if store.__class__.__name__ == "SyntheticStore":
        print("WARNING: no real market-data store found (or it's empty) — "
              "falling back to SyntheticStore. If the app is running right now, "
              "this is expected (DuckDB's file lock is exclusive) — use "
              "GET /data/event_window_signals on the running app instead.\n")

    for u in args.underlying:
        for ev in args.events:
            hh, mm = (int(x) for x in ev.split(":"))
            event_dt = datetime.combine(args.date, datetime.min.time()).replace(hour=hh, minute=mm)
            print(build_report(store, u, event_dt, args.before, args.after,
                               args.sample, args.top_strikes, args.max_age_min))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
