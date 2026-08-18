#!/usr/bin/env python3
"""Move anatomy (CLI) — what happened before every big move, historically?

Thin CLI over app/engines/move_anatomy.py (the SAME logic /data/move_anatomy
runs in-process). Discovery, not confirmation: it finds the moves first, then
ranks which features preceded them.

Reports BOTH numbers, because either alone misleads:
  recall    — of all the moves, how many had this feature beforehand
  precision — of all the times the feature fired, how many led to a move
and 'lift' = precision / base rate. Lift 1.0x means the feature added nothing,
however good its recall looks.

READ-ONLY. DuckDB's file lock is exclusive (readers included), so this can only
see real data when the app is NOT running. While it is live, use
GET /data/move_anatomy.

Usage:
    venv/bin/python -m scripts.move_anatomy --start 2026-06-01 --end 2026-08-17
    venv/bin/python -m scripts.move_anatomy --start 2026-06-01 --end 2026-08-17 \\
        --underlying NIFTY --window 30 --min-atr 2.0 --min-pct 0.35
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data.store import get_store                   # noqa: E402
from app.engines.move_anatomy import build_report       # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=date.fromisoformat, required=True)
    ap.add_argument("--end", type=date.fromisoformat, default=date.today())
    ap.add_argument("--underlying", nargs="+", default=["NIFTY", "BANKNIFTY"])
    ap.add_argument("--lookback", type=int, default=9,
                    help="bars of run-up to measure (9 x 5min = 45min)")
    ap.add_argument("--window", type=int, default=30,
                    help="minutes over which a move is measured")
    ap.add_argument("--min-atr", type=float, default=2.0,
                    help="move must be >= this many ATRs (risk-reward floor)")
    ap.add_argument("--min-pct", type=float, default=0.35,
                    help="move must ALSO be >= this %% (stops a quiet day "
                         "promoting noise via collapsed ATR)")
    ap.add_argument("--sample", type=int, default=5)
    ap.add_argument("--max-age-min", type=int, default=10)
    args = ap.parse_args()

    store = get_store()
    if store.__class__.__name__ == "SyntheticStore":
        print("WARNING: no real market-data store found (or it's empty) — "
              "falling back to SyntheticStore. If the app is running, use "
              "GET /data/move_anatomy instead (DuckDB's lock is exclusive).")
    for u in args.underlying:
        print(build_report(store, u, args.start, args.end, args.lookback,
                           args.window, args.min_atr, args.min_pct,
                           args.sample, args.max_age_min))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
