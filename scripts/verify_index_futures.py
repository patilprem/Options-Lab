"""
Verify the index-futures VOLUME companion (step 4, gated off by default)
=======================================================================
Read-only market-data check — NO orders, no ledger writes. Thin CLI over
app/engines/index_vol_check.py (the SAME logic the app runs in-process via
MarketHub._index_vol_check_loop and reports over ntfy). Run ON THE VPS (static
IP registered, creds present) DURING NSE market hours to confirm, before
flipping `index_futures_volume` on:

  1. resolve_index_futures() picks each index's real front-month FUTIDX id.
  2. paper.py's feed-mode/segment ints match the SDK's constants.
  3. A FULL subscription to that future streams a cumulative, increasing
     `volume` and an `OI` field — what LiveFeed._volume_delta consumes.

Usage:
    venv/bin/python -m scripts.verify_index_futures            # NIFTY, BANKNIFTY
    venv/bin/python -m scripts.verify_index_futures --symbols NIFTY --seconds 60

Exit code 0 = all three checks passed. Nothing here changes app state (running
it in-app additionally records events + pushes ntfy; this CLI just prints).
"""

from __future__ import annotations

import argparse

from app.engines.index_vol_check import run_full_check


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", nargs="+", default=["NIFTY", "BANKNIFTY"])
    ap.add_argument("--seconds", type=int, default=45)
    args = ap.parse_args()

    print("=" * 70)
    print("Index-futures volume companion — verification (read-only)")
    print("=" * 70)
    res = run_full_check(args.symbols, args.seconds)
    for line in res["lines"]:
        print(line)
    print("\n" + "=" * 70)
    print("VERDICT:", "ALL CHECKS PASSED — safe to set index_futures_volume=on"
          if res["passed"] else "NOT READY — fix the items flagged above first")
    print("=" * 70)
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
