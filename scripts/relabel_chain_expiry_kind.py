"""
Relabel the chain rows that were recorded as WEEKLY but hold MONTHLY contracts.
=============================================================================
    venv/bin/python -m scripts.relabel_chain_expiry_kind              # dry run
    venv/bin/python -m scripts.relabel_chain_expiry_kind --yes        # do it

THE BUG
-------
MarketHub.CHAIN_TARGETS asks every configured underlying for ("WEEKLY", 0) and
("WEEKLY", 1). NSE discontinued BANKNIFTY's weeklies and MCX options are
monthly-only, so for BANKNIFTY / CRUDEOIL / GOLD there is no weekly cycle at
all — but chain.resolve_expiry answers ("WEEKLY", 0) with the nearest listed
expiry regardless, which for those three is their MONTHLY contract. So their
chains were cached, recorded and PERSISTED under expiry_kind="WEEKLY" while
actually holding monthly contracts, and ("WEEKLY", 1) resolved ~2 months out
and was skipped: one expiry per name, wrongly labelled, and never a second.

The live path is fixed (chain.effective_targets derives the kind from the
expiry list). This relabels the history that was already written.

WHICH ROWS
----------
Data-driven, never a hardcoded list of names — BANKNIFTY genuinely HAD weeklies
until NSE dropped them, so its older rows are correctly labelled and must not
be touched. A recorded expiry is judged monthly-only when either:

  * its nearest recorded NEIGHBOUR expiry is more than WEEKLY_GAP_MAX_DAYS
    away (a weekly cycle advances every ~7 days, 14 across a holiday), or
  * it stayed the offset-0 expiry for a SPAN longer than that (a weekly is
    current for about a week; a monthly for about a month). This is what
    decides a name whose history only ever recorded ONE expiry, where the
    neighbour test has nothing to compare against.

The threshold is imported from app.engines.chain, the same constant the live
poller uses, so the migration and the recorder cannot drift apart about what
"weekly" means. Anything the two rules can't settle is REPORTED and left
alone: under-claiming is recoverable, relabelling a real weekly is not.

WHAT MAKES THIS SAFE TO RUN
---------------------------
Every affected row is written to Parquet BEFORE anything changes, and the
update is refused if that backup does not read back at the exact row count.
The closing lines print the restore for each table. An UPDATE also cannot
collide with an existing MONTHLY row at the same primary key — that is checked
first and aborts the run rather than raising halfway through.

Dry run is the DEFAULT and reads a COPY of the database, so the service can
stay up while you look. `--yes` needs the service stopped (DuckDB is
single-writer) and says so rather than failing on a lock error.

AFTER RUNNING
-------------
Strategies and backtests that declare expiry_kind=WEEKLY on a relabelled
underlying now read an empty series from the store — that combination no
longer exists, which is the point. The live path translates them
(MarketHub._effective_leg) so nothing breaks mid-session, but the declarations
should be changed to MONTHLY. The report names every underlying affected.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import sys
import tempfile
from datetime import datetime

from app.engines.chain import WEEKLY_GAP_MAX_DAYS

ROOT = os.environ.get("OPTIONSLAB_ROOT", "/opt/optionslab")
DB = os.path.join(ROOT, "marketdata.duckdb")

# table -> the primary-key columns OTHER than expiry_kind. Relabelling moves a
# row within its own table's PK space, so these are what a collision is checked
# against. option_bars keys on strike_offset, chain_snapshots on strike.
TABLES = {
    "option_bars": ("underlying", "ts", "expiry_offset", "strike_offset",
                    "option_type"),
    "chain_snapshots": ("underlying", "ts", "expiry_offset", "strike",
                        "option_type"),
}


# ---------------------------------------------------------------------------
# the rule (pure — unit-tested in tests/test_expiry_relabel.py)
# ---------------------------------------------------------------------------

def monthly_only_expiries(recorded, max_gap_days: int = WEEKLY_GAP_MAX_DAYS):
    """Which recorded expiries sit in a MONTHLY-only regime?

    `recorded` is [(expiry_date, first_trade_date, last_trade_date)] — one
    entry per distinct expiry seen under expiry_kind='WEEKLY' for a single
    underlying. Returns the subset that cannot be weekly, plus the ones the
    evidence can't settle:

        (monthly_only: list[date], undecided: list[date])

    Two independent tests, either one sufficient:

      NEIGHBOUR — a weekly expiry always has another weekly within ~7 days
      (14 if a holiday ate a week). If the nearest recorded expiry either side
      is farther than max_gap_days, this was never part of a weekly cycle.

      SPAN — a weekly is the front contract for about a week. If one expiry
      stayed the recorded offset-0 for longer than max_gap_days, it is a
      monthly. This is the test that decides a name whose whole history holds
      a single expiry, where NEIGHBOUR has nothing to compare against.

    A regime CHANGE (BANKNIFTY's last weekly followed by its first monthly)
    can leave the boundary expiry within max_gap_days of its weekly
    predecessor; SPAN normally still catches it, and when it doesn't the
    expiry lands in `undecided` rather than being guessed at.
    """
    rows = sorted((e, f, l) for e, f, l in recorded if e is not None)
    monthly, undecided = [], []
    for i, (exp, first, last) in enumerate(rows):
        gaps = []
        if i > 0:
            gaps.append(abs((exp - rows[i - 1][0]).days))
        if i + 1 < len(rows):
            gaps.append(abs((rows[i + 1][0] - exp).days))
        by_neighbour = bool(gaps) and min(gaps) > max_gap_days
        span = (last - first).days if (first and last) else None
        by_span = span is not None and span > max_gap_days
        if by_neighbour or by_span:
            monthly.append(exp)
        elif not gaps:
            # the only expiry on record, held too briefly for SPAN to speak
            undecided.append(exp)
    return monthly, undecided


# ---------------------------------------------------------------------------
# db helpers
# ---------------------------------------------------------------------------

def service_running() -> bool:
    import subprocess
    try:
        out = subprocess.run(["systemctl", "is-active", "optionslab"],
                             capture_output=True, text=True, timeout=15)
        return out.stdout.strip() == "active"
    except Exception:
        return False        # can't tell (no systemd here) — the lock will say


def connect_copy():
    """DuckDB copy — the app holds the write lock, and the file is owned by the
    service user, so a direct connect fails for anyone else."""
    tmp = tempfile.mkdtemp(prefix="olab-relabel-")
    for f in glob.glob(os.path.join(ROOT, "marketdata.duckdb*")):
        shutil.copy(f, tmp)
    import duckdb
    return duckdb.connect(os.path.join(tmp, "marketdata.duckdb")), tmp


def recorded_weekly(con, table: str, underlying: str = "") -> dict:
    """{underlying: [(expiry, first_trade_day, last_trade_day, rows)]} for every
    row currently labelled WEEKLY. Rows with a NULL expiry can't be judged (the
    backfill leaves it NULL) and are counted separately by null_expiry_rows."""
    sql = (f"SELECT underlying, expiry, min(CAST(ts AS DATE)), "   # noqa: S608
           f"max(CAST(ts AS DATE)), count(*) FROM {table} "
           f"WHERE expiry_kind = 'WEEKLY' AND expiry IS NOT NULL")
    params = []
    if underlying:
        sql += " AND underlying = ?"
        params.append(underlying)
    sql += " GROUP BY 1, 2"
    out: dict = {}
    for u, exp, first, last, n in con.execute(sql, params).fetchall():
        out.setdefault(u, []).append((exp, first, last, n))
    return out


def null_expiry_rows(con, table: str, underlying: str = "") -> dict:
    """WEEKLY rows with no expiry recorded — unjudgeable, always left alone."""
    sql = (f"SELECT underlying, count(*) FROM {table} "            # noqa: S608
           f"WHERE expiry_kind = 'WEEKLY' AND expiry IS NULL")
    params = []
    if underlying:
        sql += " AND underlying = ?"
        params.append(underlying)
    sql += " GROUP BY 1"
    return dict(con.execute(sql, params).fetchall())


def _where(table: str):
    return (f"{table}.expiry_kind = 'WEEKLY' AND {table}.underlying = ? "
            f"AND {table}.expiry IN (SELECT UNNEST(?))")


def affected(con, table: str, u: str, expiries: list) -> int:
    return con.execute(
        f"SELECT count(*) FROM {table} WHERE {_where(table)}",     # noqa: S608
        [u, expiries]).fetchone()[0]


def collisions(con, table: str, u: str, expiries: list) -> int:
    """Rows that already have a MONTHLY twin at the same primary key. The
    UPDATE would violate the PK, so a nonzero count aborts the run."""
    on = " AND ".join(f"m.{c} = w.{c}" for c in TABLES[table])
    return con.execute(
        f"SELECT count(*) FROM {table} w WHERE "                   # noqa: S608
        f"w.expiry_kind = 'WEEKLY' AND w.underlying = ? "
        f"AND w.expiry IN (SELECT UNNEST(?)) AND EXISTS ("
        f"SELECT 1 FROM {table} m WHERE m.expiry_kind = 'MONTHLY' AND {on})",
        [u, expiries]).fetchone()[0]


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--underlying", default="",
                    help="limit to one name (default: examine all)")
    ap.add_argument("--yes", action="store_true",
                    help="actually relabel (default is a dry run)")
    ap.add_argument("--backup-dir",
                    default=os.path.join(ROOT, "relabel_backups"))
    args = ap.parse_args()

    import duckdb

    if args.yes and service_running():
        print("REFUSING: the optionslab service is active and holds the DuckDB\n"
              "write lock. Stop it first, then re-run:\n"
              "    sudo systemctl stop optionslab\n"
              "    venv/bin/python -m scripts.relabel_chain_expiry_kind --yes\n"
              "    sudo systemctl start optionslab")
        return 1

    tmp = None
    try:
        if args.yes:
            con = duckdb.connect(DB, read_only=False)
        else:
            con, tmp = connect_copy()
    except Exception as e:
        print(f"cannot open {DB}: {e!r}\n"
              "(if the service is running, stop it first — DuckDB is "
              "single-writer)")
        return 1

    try:
        return run(con, args)
    finally:
        con.close()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def run(con, args) -> int:
    # plan: {table: {underlying: [expiries]}} + the row counts behind it
    plan: dict = {}
    counts: dict = {}
    total = 0
    touched_names: set = set()

    for table in TABLES:
        try:
            groups = recorded_weekly(con, table, args.underlying)
            nulls = null_expiry_rows(con, table, args.underlying)
        except Exception as e:
            print(f"{table}: cannot read ({type(e).__name__}: {e}) — skipping")
            continue
        print(f"\n=== {table} ===")
        if not groups and not nulls:
            print("  no WEEKLY-labelled rows")
            continue
        for u in sorted(set(groups) | set(nulls)):
            rows = groups.get(u, [])
            monthly, undecided = monthly_only_expiries(
                [(e, f, l) for e, f, l, _ in rows])
            n = affected(con, table, u, monthly) if monthly else 0
            weekly_kept = len(rows) - len(monthly) - len(undecided)
            print(f"  {u:<12} expiries={len(rows):<3} "
                  f"monthly-only={len(monthly):<3} weekly={weekly_kept:<3} "
                  f"undecided={len(undecided):<3} rows_to_relabel={n}")
            if undecided:
                print(f"      undecided (left as WEEKLY, too little history "
                      f"to judge): {', '.join(str(d) for d in undecided)}")
            if nulls.get(u):
                print(f"      {nulls[u]} row(s) with NULL expiry — "
                      f"unjudgeable, left as WEEKLY")
            if not monthly:
                continue
            c = collisions(con, table, u, monthly)
            if c:
                print(f"      ABORT: {c} row(s) already have a MONTHLY twin at "
                      f"the same primary key. Relabelling would violate it — "
                      f"nothing changed.")
                return 1
            plan.setdefault(table, {})[u] = monthly
            counts[(table, u)] = n
            total += n
            touched_names.add(u)

    if not total:
        print("\nnothing to relabel.")
        return 0

    print(f"\n{total} row(s) across {len(touched_names)} underlying(s): "
          f"{', '.join(sorted(touched_names))}")

    if not args.yes:
        print("\nDRY RUN — nothing changed (read from a COPY, so the service "
              "can stay up).")
        print("Re-run with --yes, service stopped, to back up and relabel.")
        return 0

    # --- backup BEFORE touching anything ------------------------------------
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(args.backup_dir, stamp)
    os.makedirs(outdir, exist_ok=True)
    print(f"\nbacking up to {outdir}")
    for table, per_u in plan.items():
        for u, expiries in per_u.items():
            want = counts[(table, u)]
            path = os.path.join(outdir, f"{table}.{u}.parquet")
            con.execute(
                f"COPY (SELECT * FROM {table} WHERE {_where(table)}) "  # noqa: S608
                f"TO '{path}' (FORMAT PARQUET)", [u, expiries])
            back = con.execute(
                "SELECT count(*) FROM read_parquet(?)", [path]).fetchone()[0]
            if back != want:
                print(f"  ABORT: {table}/{u} backup has {back} rows, expected "
                      f"{want} — nothing relabelled.")
                return 1
            print(f"  {table}.{u:<12} {back} rows -> {os.path.basename(path)}")

    # --- relabel, then verify -----------------------------------------------
    print("\nrelabelling")
    for table, per_u in plan.items():
        for u, expiries in per_u.items():
            con.execute(
                f"UPDATE {table} SET expiry_kind = 'MONTHLY' "          # noqa: S608
                f"WHERE {_where(table)}", [u, expiries])
    left = 0
    for table, per_u in plan.items():
        for u, expiries in per_u.items():
            n = affected(con, table, u, expiries)
            left += n
            print(f"  {table}.{u:<12} remaining WEEKLY {n}")
    if left:
        print("  WARNING: rows survived the update — investigate before "
              "restarting the service.")
        return 1

    print(f"\ndone. {total} row(s) relabelled WEEKLY -> MONTHLY, "
          f"backed up in {outdir}")
    print("to restore, with the service stopped:")
    for table, per_u in plan.items():
        for u in per_u:
            path = os.path.join(outdir, f"{table}.{u}.parquet")
            on = " AND ".join(f"t.{c} = b.{c}" for c in TABLES[table])
            print(f"    DELETE FROM {table} t USING read_parquet('{path}') b "
                  f"WHERE t.expiry_kind = 'MONTHLY' AND {on};")
            print(f"    INSERT INTO {table} SELECT * FROM "
                  f"read_parquet('{path}');")
    print(f"\nNOTE: strategies declaring expiry_kind=WEEKLY on "
          f"{', '.join(sorted(touched_names))} should be changed to MONTHLY — "
          f"that combination no longer exists in the store. The live path "
          f"translates them, backtests read an empty series.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
