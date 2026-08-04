"""
Which MCX days recorded a contract nobody was trading?
======================================================
    venv/bin/python -m scripts.mcx_contract_audit

Until 2026-08-04, resolve_mcx_ids picked the NEAREST NON-EXPIRED futures
contract. Liquidity rolls weeks before expiry, so for the tail of every
contract's life the recorder was pointed at one the market had already left:
GOLD spent 2026-08-04 on GOLD-05Aug2026-FUT, expiring the next day, printing no
WS ticks for 75+ minutes and serving a chain last_price frozen at 142204 for
six hours. Those days' underlying_bars and chain_snapshots are WRONG, not
merely thin — a backtest reading them sees a price that cannot move.

The fix (pick_active_contract, by open interest) stops it recurring. It does
nothing about what is already on disk, and nothing in the schema records WHICH
contract a row came from, so the damage has to be inferred from the data.

METHOD — measure against a control, not a constant. CRUDEOIL and GOLD share a
session, a feed and a recorder, so on any given day CRUDEOIL says what "fully
recorded" looked like. A GOLD day with a fraction of CRUDEOIL's bars, or a
chain whose spot barely moves, was a GOLD problem, not a quiet market. No
absolute thresholds to re-tune when the recorder's uptime changes.

READ-ONLY. It prints a verdict and a suggested date range; it does not delete,
rewrite or mark anything. Deleting recorded market data is a decision to take
deliberately, with these numbers in front of you — not a side effect of an
audit. See the closing note for what removal would involve.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import sys
import tempfile

ROOT = os.environ.get("OPTIONSLAB_ROOT", "/opt/optionslab")

# A day whose bar count falls below this fraction of the control's is suspect.
# Generous: a genuinely thin contract still prints most buckets while it is the
# one being traded — GOLD ran at ~30% of CRUDEOIL on the day it was confirmed
# dead, and at full parity in the weeks before.
COVERAGE_SUSPECT = 0.60
# A chain whose spot takes this few distinct values across a whole session is
# not being traded. 2026-08-04 GOLD: two, in six hours.
SPOT_DISTINCT_SUSPECT = 5


def connect():
    """DuckDB copy — the app holds the write lock, and the file is owned by the
    service user, so a direct connect fails for anyone else."""
    tmp = tempfile.mkdtemp(prefix="olab-mcx-audit-")
    for f in glob.glob(os.path.join(ROOT, "marketdata.duckdb*")):
        shutil.copy(f, tmp)
    import duckdb
    return duckdb.connect(os.path.join(tmp, "marketdata.duckdb")), tmp


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="GOLD", help="MCX name to audit")
    ap.add_argument("--control", default="CRUDEOIL",
                    help="MCX name to measure it against")
    args = ap.parse_args()

    try:
        con, tmp = connect()
    except Exception as e:
        print(f"cannot read the market store: {e!r}")
        return 1
    try:
        bars = dict(con.execute(
            "SELECT CAST(ts AS DATE) d, count(*) FROM underlying_bars "
            "WHERE underlying = ? GROUP BY 1", [args.name]).fetchall())
        ctrl = dict(con.execute(
            "SELECT CAST(ts AS DATE) d, count(*) FROM underlying_bars "
            "WHERE underlying = ? GROUP BY 1", [args.control]).fetchall())
        spot = {r[0]: (r[1], r[2], r[3]) for r in con.execute(
            "SELECT CAST(ts AS DATE) d, count(DISTINCT spot), min(spot), max(spot) "
            "FROM chain_snapshots WHERE underlying = ? GROUP BY 1",
            [args.name]).fetchall()}
    finally:
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)

    days = sorted(set(bars) | set(ctrl) | set(spot))
    if not days:
        print(f"no recorded days for {args.name}")
        return 1

    print(f"{args.name} vs {args.control} — bar coverage and chain movement\n")
    print(f"{'date':<12}{'bars':>7}{'ctrl':>7}{'cover':>8}"
          f"{'spots':>7}{'spot range':>14}  verdict")
    suspect = []
    for d in days:
        n, c = bars.get(d, 0), ctrl.get(d, 0)
        cover = (n / c) if c else None
        ds, lo, hi = spot.get(d, (0, None, None))
        rng = f"{(hi - lo):,.0f}" if lo is not None and hi is not None else "-"
        why = []
        if cover is not None and cover < COVERAGE_SUSPECT:
            why.append("thin bars")
        if ds and ds <= SPOT_DISTINCT_SUSPECT:
            why.append("frozen spot")
        if why:
            suspect.append(d)
        print(f"{str(d):<12}{n:>7}{c:>7}"
              f"{(f'{cover:.0%}' if cover is not None else '-'):>8}"
              f"{ds:>7}{rng:>14}  {', '.join(why) or 'ok'}")

    print()
    if not suspect:
        print(f"No suspect days: {args.name} tracked a traded contract "
              f"throughout. Nothing to clean up.")
        return 0

    print(f"SUSPECT: {len(suspect)} day(s), {suspect[0]} .. {suspect[-1]}")
    print(f"  On these, {args.name}'s underlying_bars and chain_snapshots very "
          f"likely describe a contract that had already been rolled out of.")
    print(f"  The prices are real quotes, but of an instrument nobody was "
          f"trading, so any backtest over this range is reading a stale book.")
    print()
    print("  NOT deleted. Two things to weigh first:")
    print("   * MCX expired-option data cannot be re-fetched from Dhan — we are"
          " the only recorder — so anything removed here is gone for good.")
    print("   * Missing days are handled by the engines; WRONG days are not."
          " That argues for removal, but it is your call, not an audit's.")
    print()
    print("  To remove, once you have decided (per table, per day):")
    print(f"    DELETE FROM underlying_bars  WHERE underlying='{args.name}'"
          f" AND CAST(ts AS DATE) IN (...);")
    print(f"    DELETE FROM chain_snapshots  WHERE underlying='{args.name}'"
          f" AND CAST(ts AS DATE) IN (...);")
    print(f"    DELETE FROM option_bars      WHERE underlying='{args.name}'"
          f" AND CAST(ts AS DATE) IN (...);")
    print("  Stop the service first — it holds the DuckDB write lock.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
