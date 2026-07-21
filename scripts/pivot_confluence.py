#!/usr/bin/env python3
"""Multi-index pivot-confluence report (CLI).

Thin CLI over app/engines/confluence.py (the SAME logic the
/data/pivot_confluence API endpoint runs in-process). Checks whether every
given underlying is currently within `--tolerance-pct` of one of its own
prior-session pivot levels (classic floor pivots + CPR), plus a reversal-candle
read (range position / outside bar / break-of-structure / RSI) and a chain
(PCR/IV-skew/max-pain) + index_bias read at that moment.

READ-ONLY. Like scripts/event_window_signals.py, this can only see real data
when the app process is NOT running (DuckDB's file lock is exclusive, reads
included) — use GET /data/pivot_confluence while the app is live.

Usage:
    venv/bin/python -m scripts.pivot_confluence
    venv/bin/python -m scripts.pivot_confluence --at "2026-07-21 12:30" \\
        --underlying NIFTY BANKNIFTY --tolerance-pct 0.15
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data.store import get_store                          # noqa: E402
from app.engines.confluence import build_confluence_report     # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--at", type=str, default=None,
                    help="as-of timestamp 'YYYY-MM-DD HH:MM' (IST, default: now)")
    ap.add_argument("--underlying", nargs="+", default=["NIFTY", "BANKNIFTY"])
    ap.add_argument("--tolerance-pct", type=float, default=0.15,
                    help="how close to a pivot level counts as 'near', as %% of spot")
    ap.add_argument("--lookback-days", type=int, default=5,
                    help="history window to find the prior session in (padded for weekends)")
    ap.add_argument("--max-age-min", type=int, default=10,
                    help="reject a chain/bias reading older than this many minutes")
    args = ap.parse_args()

    asof = datetime.strptime(args.at, "%Y-%m-%d %H:%M") if args.at else datetime.now()

    store = get_store()
    if store.__class__.__name__ == "SyntheticStore":
        print("WARNING: no real market-data store found (or it's empty) — "
              "falling back to SyntheticStore. If the app is running right now, "
              "this is expected (DuckDB's file lock is exclusive) — use "
              "GET /data/pivot_confluence on the running app instead.\n")

    print(build_confluence_report(store, args.underlying, asof, args.tolerance_pct,
                                  args.lookback_days, args.max_age_min))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
