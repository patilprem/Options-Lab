"""
Retroactive index VOLUME backfill
=================================
Fill volume/OI into historical NIFTY/BANKNIFTY `underlying_bars` from the
front-month index FUTURE that traded on each of those days.

Why: indexes have no volume of their own, so every index bar recorded before
the futures companion was switched on (2026-07-27) carries volume=0. `vwap()`
and every volume-aware signal silently degraded to an unweighted average over
that entire history. The companion fixes it forward; this fixes it backward,
so the recorded sessions become usable for volume-aware backtests.

SAFE BY CONSTRUCTION:
  * price is never touched — only volume/OI, only on bars that already exist
  * bars that already carry volume are skipped, so re-running is harmless and
    live companion data can never be overwritten
  * a day whose front-month contract has expired and left the scrip master is
    REPORTED, not guessed at (see app/data/index_volume.py, rule 2)

Run ON THE VPS (creds + registered static IP). Read-only against Dhan; the
only writes are volume/OI updates to underlying_bars.

Usage:
    venv/bin/python -m scripts.backfill_index_volume --dry-run
    venv/bin/python -m scripts.backfill_index_volume
    venv/bin/python -m scripts.backfill_index_volume --from 2026-07-14 --to 2026-07-22
    venv/bin/python -m scripts.backfill_index_volume --symbols NIFTY
"""

from __future__ import annotations

import argparse
from datetime import date, datetime

from app.data import dhan_client as dc
from app.data.index_volume import contract_spans, volume_rows_from_intraday

DEFAULT_SYMBOLS = ("NIFTY", "BANKNIFTY")


def _zero_volume_days(store, underlying: str, d_from, d_to) -> list:
    """Sessions where this underlying has bars but no volume — the work list."""
    sql = """SELECT DISTINCT ts::DATE AS d FROM underlying_bars
             WHERE underlying = ? AND (volume IS NULL OR volume = 0)"""
    params = [underlying]
    if d_from:
        sql += " AND ts::DATE >= ?"
        params.append(d_from)
    if d_to:
        sql += " AND ts::DATE <= ?"
        params.append(d_to)
    return [r[0] for r in store.con.execute(sql + " ORDER BY d", params).fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                    help="comma-separated (default: NIFTY,BANKNIFTY)")
    ap.add_argument("--from", dest="d_from", help="YYYY-MM-DD (default: all)")
    ap.add_argument("--to", dest="d_to", help="YYYY-MM-DD (default: all)")
    ap.add_argument("--interval", type=int, default=5,
                    help="candle minutes; must match the stored bars (default 5)")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve contracts and report the plan, fetch nothing")
    args = ap.parse_args()

    d_from = datetime.fromisoformat(args.d_from).date() if args.d_from else None
    d_to = datetime.fromisoformat(args.d_to).date() if args.d_to else None

    from app.data.store import DataStore
    store = DataStore()
    master = dc.index_fut_master_rows()
    client = None if args.dry_run else dc.get_client()

    total_updated = 0
    for name in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        days = _zero_volume_days(store, name, d_from, d_to)
        if not days:
            print(f"{name}: nothing to do (no zero-volume bars in range)")
            continue

        spans, unresolved = contract_spans(master, name, days)
        print(f"\n{name}: {len(days)} session(s) with zero volume "
              f"[{days[0]} .. {days[-1]}]")
        if unresolved:
            print(f"  SKIPPED {len(unresolved)} day(s) — front-month contract no "
                  f"longer in the scrip master, refusing to guess:")
            print(f"    {unresolved[0]} .. {unresolved[-1]}")

        for sp in spans:
            span_days = {d for d in days
                         if sp["first_day"] <= d <= sp["last_day"]}
            label = (f"  {sp['first_day']}..{sp['last_day']} "
                     f"sid={sp['security_id']} expiry={sp['expiry']} "
                     f"({len(span_days)} session(s))")
            if args.dry_run:
                print(label + "  [dry-run]")
                continue

            segment = (dc.UNDERLYINGS.get(name, {}).get("fno_segment")
                       or "NSE_FNO")
            try:
                data = dc.fetch_intraday(
                    client, sp["security_id"], segment, "FUTIDX",
                    args.interval, str(sp["first_day"]), str(sp["last_day"]),
                    oi=True)
            except Exception as e:
                print(label + f"  FETCH FAILED: {e!r}")
                continue

            rows = volume_rows_from_intraday(name, data, span_days)
            n = store.update_bar_volume(rows)
            total_updated += n
            print(label + f"  fetched={len(rows)} updated={n}")
            if rows and not n:
                print("     (0 updated — timestamps did not line up with stored "
                      "bars; check --interval matches how they were recorded)")

    print(f"\nTOTAL bars given volume: {total_updated}")
    if args.dry_run:
        print("dry-run: nothing was fetched or written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
