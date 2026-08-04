"""
Remove the MCX days that recorded a contract nobody was trading.
===============================================================
    venv/bin/python -m scripts.mcx_purge_dead_contract              # dry run
    venv/bin/python -m scripts.mcx_purge_dead_contract --yes        # do it

Companion to scripts.mcx_contract_audit, which explains WHY these days are
wrong. The suspect-day rule is imported from it rather than restated, so the
report and the DELETE can never disagree about what gets removed.

WHAT MAKES THIS SAFE TO RUN
---------------------------
Every affected row is written to Parquet BEFORE anything is deleted, and the
delete is refused if that backup does not read back with the exact row count.
So this is reversible: the closing lines print the one-line restore for each
table. That matters more here than almost anywhere else in the codebase —
Dhan serves no expired-option history for MCX, we are the only recorder, and a
mistaken DELETE would otherwise be permanent.

Dry run is the DEFAULT and prints exactly what would go, per table per day. It
reads a COPY of the database, so the service can stay up while you look — the
first version pointed the preview at the live file, and since DuckDB refuses
even a read_only open against a held write lock, that meant taking recording
down just to see what a delete WOULD do. A dry run you have to stop the world
to run is one people skip.

`--yes` does need the service stopped, and refuses early with the stop/start
commands rather than failing halfway through on a lock error.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime

from scripts.mcx_contract_audit import collect, reasons, suspect_days

ROOT = os.environ.get("OPTIONSLAB_ROOT", "/opt/optionslab")
DB = os.path.join(ROOT, "marketdata.duckdb")
TABLES = ("underlying_bars", "option_bars", "chain_snapshots")


def service_running() -> bool:
    try:
        out = subprocess.run(["systemctl", "is-active", "optionslab"],
                             capture_output=True, text=True, timeout=15)
        return out.stdout.strip() == "active"
    except Exception:
        return False        # can't tell (no systemd here) — the lock will say


def counts(con, name: str, days: list) -> dict:
    """Rows that would be removed, per table. Uses the SAME date predicate the
    delete will use, so the preview cannot understate the damage."""
    out = {}
    for t in TABLES:
        try:
            out[t] = con.execute(
                f"SELECT count(*) FROM {t} WHERE underlying = ? "      # noqa: S608
                f"AND CAST(ts AS DATE) IN (SELECT UNNEST(?))",
                [name, days]).fetchone()[0]
        except Exception as e:
            out[t] = f"<{type(e).__name__}: {e}>"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="GOLD")
    ap.add_argument("--control", default="CRUDEOIL")
    ap.add_argument("--yes", action="store_true",
                    help="actually delete (default is a dry run)")
    ap.add_argument("--backup-dir", default=os.path.join(ROOT, "purge_backups"))
    ap.add_argument("--days", default="",
                    help="comma-separated YYYY-MM-DD to override the audit's "
                         "verdict — use only to purge FEWER days than it found")
    args = ap.parse_args()

    import duckdb

    if args.yes and service_running():
        print("REFUSING: the optionslab service is active and holds the DuckDB\n"
              "write lock. Stop it first, then re-run:\n"
              "    sudo systemctl stop optionslab\n"
              "    venv/bin/python -m scripts.mcx_purge_dead_contract --yes\n"
              "    sudo systemctl start optionslab")
        return 1

    # A DRY RUN MUST NEVER NEED THE SERVICE STOPPED. DuckDB refuses even a
    # read_only open while another process holds the write lock, so pointing
    # the preview at the live file meant you had to take recording down just to
    # LOOK at what would be deleted — the exact opposite of what a dry run is
    # for, and it would push anyone toward skipping straight to --yes. The
    # preview reads a copy (same trick as the audit and scripts/diag.py); only
    # the real delete touches the live database.
    tmp = None
    try:
        if args.yes:
            con = duckdb.connect(DB, read_only=False)
        else:
            from scripts.mcx_contract_audit import connect as audit_connect
            con, tmp = audit_connect()
    except Exception as e:
        print(f"cannot open {DB}: {e!r}\n"
              "(if the service is running, stop it first — DuckDB is "
              "single-writer)")
        return 1

    try:
        rows = collect(con, args.name, args.control)
        if not rows:
            print(f"no recorded days for {args.name}")
            return 1
        if args.days:
            want = {d.strip() for d in args.days.split(",") if d.strip()}
            days = [r[0] for r in rows if str(r[0]) in want]
            missing = want - {str(d) for d in days}
            if missing:
                print(f"not recorded, ignoring: {sorted(missing)}")
        else:
            days = suspect_days(rows)

        if not days:
            print(f"{args.name}: no suspect days — nothing to remove.")
            return 0

        print(f"{args.name}: {len(days)} suspect day(s)\n")
        by_day = {r[0]: r for r in rows}
        for d in days:
            r = by_day[d]
            print(f"  {d}  bars={r[1]:<5} vs control {r[2]:<5} "
                  f"cover={(f'{r[3]:.0%}' if r[3] is not None else '-'):<5} "
                  f"spots={r[4]:<4} [{', '.join(reasons(r))}]")

        n = counts(con, args.name, days)
        print("\nrows that would be removed:")
        for t in TABLES:
            print(f"  {t:<18} {n[t]}")
        total = sum(v for v in n.values() if isinstance(v, int))
        if not total:
            print("\nnothing matches — no rows to remove.")
            return 0

        if not args.yes:
            print(f"\nDRY RUN — nothing changed (read from a COPY, so the "
                  f"service can stay up). {total} rows would go.")
            print("Re-run with --yes, service stopped, to back up and delete.")
            return 0

        # --- backup BEFORE touching anything --------------------------------
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        outdir = os.path.join(args.backup_dir, f"{args.name}-{stamp}")
        os.makedirs(outdir, exist_ok=True)
        print(f"\nbacking up to {outdir}")
        for t in TABLES:
            if not isinstance(n[t], int) or not n[t]:
                continue
            path = os.path.join(outdir, f"{t}.parquet")
            con.execute(
                f"COPY (SELECT * FROM {t} WHERE underlying = ? "          # noqa: S608
                f"AND CAST(ts AS DATE) IN (SELECT UNNEST(?))) "
                f"TO '{path}' (FORMAT PARQUET)", [args.name, days])
            back = con.execute(
                "SELECT count(*) FROM read_parquet(?)", [path]).fetchone()[0]
            if back != n[t]:
                print(f"  ABORT: {t} backup has {back} rows, expected {n[t]} — "
                      f"nothing deleted.")
                return 1
            print(f"  {t:<18} {back} rows -> {os.path.basename(path)}")

        # --- delete, then verify --------------------------------------------
        print("\ndeleting")
        for t in TABLES:
            if not isinstance(n[t], int) or not n[t]:
                continue
            con.execute(
                f"DELETE FROM {t} WHERE underlying = ? "                  # noqa: S608
                f"AND CAST(ts AS DATE) IN (SELECT UNNEST(?))",
                [args.name, days])
        left = counts(con, args.name, days)
        for t in TABLES:
            print(f"  {t:<18} remaining {left[t]}")
        if any(isinstance(v, int) and v for v in left.values()):
            print("  WARNING: rows survived the delete — investigate before "
                  "restarting the service.")
            return 1

        print(f"\ndone. {total} rows removed, backed up in {outdir}")
        print("to restore, with the service stopped:")
        for t in TABLES:
            if isinstance(n[t], int) and n[t]:
                print(f"    INSERT INTO {t} SELECT * FROM read_parquet("
                      f"'{os.path.join(outdir, t + '.parquet')}');")
        return 0
    finally:
        con.close()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
