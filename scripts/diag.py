"""
One-shot full-system diagnostic
===============================
Everything we kept hand-assembling during the 2026-07 incident week, in one
command that is safe to run any time:

    venv/bin/python -m scripts.diag          # on the VPS

Checks: deployed sha + service state, token hours, /data/health, per-underlying
recording freshness (works around the DuckDB lock by querying a temp copy),
scanner pipeline state (sweep/Tier-2 cadence, top scores vs entry_score, books),
today's warn/error events, and ends with a RED-flag verdict list so the answer
to "is everything working?" is the LAST LINE, not an interpretation exercise.

Hard-won formatting notes baked in so nobody re-learns them:
  * events.ts uses a SPACE separator (registry.record_event isoformat(sep=" ")).
    Filtering with ...||'T09:15' compares ' ' (0x20) against 'T' (0x54) and
    silently excludes EVERYTHING — an entire day of ad-hoc queries returned
    empty because of this. Filter with LIKE 'YYYY-MM-DD %' or a space-separated
    prefix.
  * the app holds the DuckDB write lock; even read_only connects fail. Copy
    the .duckdb (+ any -wal) to a temp dir and query the copy.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, time as dtime, timedelta, timezone

ROOT = os.environ.get("OPTIONSLAB_ROOT", "/opt/optionslab")
API = os.environ.get("OPTIONSLAB_API", "http://localhost:8000")
IST = timezone(timedelta(hours=5, minutes=30))

RED: list[str] = []      # verdict accumulator


def flag(msg: str) -> None:
    RED.append(msg)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def http_json(path: str):
    try:
        with urllib.request.urlopen(f"{API}{path}", timeout=10) as r:
            return json.load(r)
    except Exception as e:
        flag(f"API {path} unreachable: {e!r}")
        return None


def run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=15).stdout.strip()
    except Exception as e:
        return f"<{e!r}>"


def nse_open(now) -> bool:
    return now.weekday() < 5 and dtime(9, 15) <= now.time() <= dtime(15, 30)


def mcx_open(now) -> bool:
    return now.weekday() < 5 and dtime(9, 0) <= now.time() <= dtime(23, 30)


def main() -> int:
    now = datetime.now(IST).replace(tzinfo=None)
    print(f"OptionsLab diagnostic — {now:%Y-%m-%d %H:%M:%S} IST  "
          f"(NSE {'OPEN' if nse_open(now) else 'closed'}, "
          f"MCX {'OPEN' if mcx_open(now) else 'closed'})")

    # -- deploy / service ----------------------------------------------------
    section("deploy")
    sha = run(["git", "-C", ROOT, "log", "--oneline", "-1"])
    active = run(["systemctl", "is-active", "optionslab"])
    print(f"  sha:     {sha}")
    print(f"  service: {active}")
    if active and active != "active" and "<" not in active:
        flag(f"service is {active}")

    # -- token ---------------------------------------------------------------
    section("token")
    tok = http_json("/token/status")
    if tok:
        hrs = tok.get("hours_left")
        print(f"  state={tok.get('state')} hours_left={hrs}")
        if tok.get("state") != "ok":
            flag(f"token state {tok.get('state')}")
        elif isinstance(hrs, (int, float)) and hrs < 2:
            flag(f"token expires in {hrs:.1f}h")

    # -- recording health (segment-aware endpoint) ---------------------------
    section("data health")
    dh = http_json("/data/health")
    if dh:
        print(f"  any_stale: {dh.get('any_stale')}")
        for t in dh.get("tables", []):
            mark = " STALE" if t.get("stale") else ""
            print(f"  {t['table']:<20} rows_today={t.get('rows_today'):<8}"
                  f" last={str(t.get('last_ts'))[:19]}{mark}")
            if t.get("stale"):
                flag(f"{t['table']} stale")

    # -- per-underlying freshness (temp copy dodges the writer's lock) -------
    section("per-underlying today (from DB copy)")
    tmp = tempfile.mkdtemp(prefix="olab-diag-")
    try:
        for f in glob.glob(os.path.join(ROOT, "marketdata.duckdb*")):
            shutil.copy(f, tmp)
        import duckdb
        con = duckdb.connect(os.path.join(tmp, "marketdata.duckdb"))
        for table in ("underlying_bars", "chain_snapshots"):
            rows = con.execute(
                f"SELECT underlying, count(*), max(ts) FROM {table} "
                f"WHERE ts::DATE = current_date "
                f"GROUP BY 1 ORDER BY 1").fetchall()
            print(f"  {table}:")
            for u, n, last in rows:
                age_min = (now - last).total_seconds() / 60.0
                print(f"    {u:<12} rows={n:<8} last={str(last)[11:19]} "
                      f"({age_min:.0f}m ago)")
            if table == "chain_snapshots":
                names = {r[0] for r in rows}
                for want in ("NIFTY", "BANKNIFTY"):
                    if nse_open(now) and want not in names:
                        flag(f"no {want} chain rows today")
                for want in ("CRUDEOIL", "GOLD"):
                    if mcx_open(now) and want not in names:
                        flag(f"no {want} chain rows today")
        con.close()
    except Exception as e:
        print(f"  (skipped: {e!r})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # -- scanner pipeline ----------------------------------------------------
    section("scanner")
    sc = http_json("/scanner")
    if sc:
        scores = sc.get("scores") or []
        print(f"  enabled={sc.get('enabled')} universe={sc.get('universe_size')}"
              f" last_sweep={str(sc.get('last_sweep'))[11:19]}")
        for s in scores[:6]:
            print(f"    {s.get('symbol'):<12} {s.get('score'):>5} "
                  f"{s.get('bias') or '-':<3} dd={s.get('deep_dived')}")
        # entry_score comes from trade settings; default mirrors TradeConfig
        q = [s for s in scores if (s.get("score") or 0) >= 65 and s.get("bias")]
        dd_q = [s for s in q if s.get("deep_dived")]
        print(f"  qualify(>=65+bias): {len(q)}  of which deep_dived: {len(dd_q)}")
        if q and not dd_q:
            flag("qualifying names have no chains (alignment broken again?)")

    # -- events: today's warn/error (SPACE-separator-safe filter) ------------
    section("today's warn/error events")
    ev_tmp = None
    try:
        import sqlite3
        # NOT a bare connect(): the DB is in WAL mode, so SQLite wants to write
        # the -shm/-wal sidecars even to READ, and a plain connect as anyone
        # but the service user dies with "attempt to write a readonly
        # database" — taking this whole section, and everything after it, with
        # it (2026-08-03, running as `ubuntu`). Copy first, same as the DuckDB
        # handling above; fall back to an immutable read-only URI.
        src = os.path.join(ROOT, "optionslab.db")
        try:
            ev_tmp = tempfile.mkdtemp(prefix="olab-diag-ev-")
            for f in glob.glob(src + "*"):
                shutil.copy(f, ev_tmp)
            db = sqlite3.connect(os.path.join(ev_tmp, "optionslab.db"))
        except Exception:
            shutil.rmtree(ev_tmp, ignore_errors=True)
            ev_tmp = None
            db = sqlite3.connect(f"file:{src}?immutable=1", uri=True)
        day = now.strftime("%Y-%m-%d")
        rows = db.execute(
            "SELECT ts, level, kind, message FROM events "
            "WHERE ts LIKE ? AND level IN ('warn','error') "
            "ORDER BY id DESC LIMIT 12", (day + "%",)).fetchall()
        for ts, lvl, kind, msg in rows:
            print(f"  {ts[11:19]} {lvl:<5} {kind:<8} {msg[:70]}")
        if not rows:
            print("  (none)")
        # tier-2 cadence: silent >15 min during NSE session is a stall
        t2 = db.execute(
            "SELECT max(ts) FROM events WHERE ts LIKE ? "
            "AND message LIKE '%tier-2%'", (day + "%",)).fetchone()[0]
        if t2:
            # events.ts is stored from a tz-AWARE datetime (registry.record_event
            # uses datetime.now(IST).isoformat(...)), so it carries a +05:30
            # offset; `now` here is naive. Strip it before subtracting or this
            # crashes with "can't subtract offset-naive and offset-aware
            # datetimes" — which it did, 2026-07-28, wiping the rest of this
            # section's output along with everything after it in the function.
            t2_dt = datetime.fromisoformat(t2).replace(tzinfo=None)
            age = (now - t2_dt).total_seconds() / 60.0
            print(f"  last tier-2 event: {t2[11:19]} ({age:.0f}m ago)")
            if nse_open(now) and age > 15:
                flag(f"tier-2 silent for {age:.0f}m during NSE session")
        elif nse_open(now):
            flag("no tier-2 events at all today")
        db.close()
    except Exception as e:
        print(f"  (skipped: {e!r})")
        flag(f"could not read the events log: {e!r}")
    finally:
        if ev_tmp:
            shutil.rmtree(ev_tmp, ignore_errors=True)

    # -- books ---------------------------------------------------------------
    section("positions")
    port = http_json("/portfolio/today")
    if port:
        openpos = port.get("open_positions") or []
        print(f"  open={len(openpos)} equity={port.get('equity')}")
        for p in openpos[:8]:
            print(f"    {json.dumps(p)[:96]}")

    # -- verdict -------------------------------------------------------------
    section("VERDICT")
    if RED:
        for r in RED:
            print(f"  RED: {r}")
        return 1
    print("  ALL CLEAR")
    return 0


if __name__ == "__main__":
    sys.exit(main())
