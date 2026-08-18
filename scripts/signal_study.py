#!/usr/bin/env python3
"""Signal validation (CLI) — is the skew/bias divergence real, or was it one day?

Thin CLI over app/engines/signal_study.py (the SAME logic /data/signal_study
runs in-process). Measures the CONDITION across every recorded session and
reports its forward returns against the UNCONDITIONAL baseline over the same
bars, plus a threshold sweep.

Read the EDGE column, not the hit rate. A rule that wins 55% of the time is
worth nothing if the market rose 55% of the time anyway.

READ-ONLY. DuckDB's file lock is exclusive (readers included), so this CLI can
only see real data when the app process is NOT running. While the app is live,
use GET /data/signal_study instead.

Usage:
    venv/bin/python -m scripts.signal_study --start 2026-06-01 --end 2026-08-17
    venv/bin/python -m scripts.signal_study --start 2026-06-01 --end 2026-08-17 \\
        --underlying NIFTY --lookback 9 --shift 0.5 --gate 0.3 --no-sweep
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data.store import get_store                  # noqa: E402
from app.engines.signal_study import build_report      # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=date.fromisoformat, required=True)
    ap.add_argument("--end", type=date.fromisoformat, default=date.today())
    ap.add_argument("--underlying", nargs="+", default=["NIFTY", "BANKNIFTY"])
    ap.add_argument("--lookback", type=int, default=9, help="bars of skew lookback")
    ap.add_argument("--shift", type=float, default=0.5, help="min skew shift")
    ap.add_argument("--gate", type=float, default=0.3, help="min |index_bias|")
    ap.add_argument("--sample", type=int, default=5)
    ap.add_argument("--max-age-min", type=int, default=10)
    ap.add_argument("--no-sweep", action="store_true",
                    help="skip the threshold sweep (much faster)")
    args = ap.parse_args()

    store = get_store()
    if store.__class__.__name__ == "SyntheticStore":
        print("WARNING: no real market-data store found (or it's empty) — "
              "falling back to SyntheticStore. If the app is running, use "
              "GET /data/signal_study instead (DuckDB's lock is exclusive).")
    for u in args.underlying:
        print(build_report(store, u, args.start, args.end, args.lookback,
                           args.shift, args.gate, sample=args.sample,
                           max_age_min=args.max_age_min,
                           do_sweep=not args.no_sweep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
