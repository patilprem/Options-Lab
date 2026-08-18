#!/usr/bin/env python3
"""Pre-move clue sweep (CLI) — was there a tell before today's big move?

Thin CLI over app/engines/premove.py (the SAME logic /data/premove_clues runs
in-process). You do NOT pass a time: it finds the day's sharpest moves from
underlying_bars itself, then scores the run-up before each one against the rest
of the same session.

READ-ONLY. DuckDB only allows one process to hold marketdata.duckdb open at a
time (readers included), so this CLI can only see real data when the app
process is NOT running. While the app is live, use GET /data/premove_clues
instead — same report, served from the app's already-open connection.

Usage:
    venv/bin/python -m scripts.premove_clues
    venv/bin/python -m scripts.premove_clues --date 2026-08-17 \\
        --underlying NIFTY BANKNIFTY --window 30 --lookback 45 --top 3
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data.store import get_store              # noqa: E402
from app.engines.premove import build_report      # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", type=date.fromisoformat, default=date.today())
    ap.add_argument("--underlying", nargs="+", default=["NIFTY", "BANKNIFTY"])
    ap.add_argument("--window", type=int, default=30,
                    help="minutes that define a 'move' (default 30)")
    ap.add_argument("--lookback", type=int, default=45,
                    help="minutes of run-up to scan before each move")
    ap.add_argument("--sample", type=int, default=5)
    ap.add_argument("--top", type=int, default=3, help="how many moves to scan")
    ap.add_argument("--max-age-min", type=int, default=10)
    args = ap.parse_args()

    store = get_store()
    if store.__class__.__name__ == "SyntheticStore":
        print("WARNING: no real market-data store found (or it's empty) — "
              "falling back to SyntheticStore. If the app is running right now, "
              "use GET /data/premove_clues instead (DuckDB's lock is exclusive).")
    for u in args.underlying:
        print(build_report(store, u, args.date, args.window, args.lookback,
                           args.sample, args.top, args.max_age_min))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
