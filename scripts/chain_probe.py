"""
Chain-outage probe — why is THIS underlying's chain not recording?
=================================================================
`scripts.diag` answers "is everything working?". This answers the one
question that has cost six sessions of chain data and has never once been
answered while the evidence was still alive:

    Dhan is answering for NIFTY and not for BANKNIFTY/CRUDEOIL/GOLD.
    What, exactly, is it saying?

    venv/bin/python -m scripts.chain_probe             # read-only
    venv/bin/python -m scripts.chain_probe --probe     # + ask Dhan directly
    venv/bin/python -m scripts.chain_probe --probe --names BANKNIFTY,GOLD

Read-only mode touches nothing live: recording freshness per underlying for
BOTH chain tables, the self-heal ladder's own timeline, and the no-op /
empty-failure lines printed IN FULL — diag truncates messages to 70
characters, which cuts off `raw_response=` exactly where it starts to matter.

--probe additionally calls Dhan itself: expiry_list, then option_chain for
the nearest expiry, printing the RAW response. That is the definitive
answer, and nothing in the codebase captured it before — `_remarks_message`
discards the payload the moment its two friendly fields come up empty, and
by the time anyone reaches the VPS the request is long gone.

CAUTION on --probe: it uses its own client, so its requests are NOT inside
MarketHub's global 3s gate. Dhan's option-chain limit is per ACCOUNT (1
unique request / 3s), so a probe running alongside a live poller can push
the pair over the limit and cause the very failures you are diagnosing.
Requests here are spaced PROBE_GAP_S (5s, deliberately slower than the app's
3s) and the default is the four core names only. Prefer --names to probe
just the ones that are actually dark.

Formatting notes inherited from scripts/diag.py, do not re-learn them:
  * events.ts uses a SPACE separator; filter with LIKE 'YYYY-MM-DD %'.
  * events.ts is written from a tz-AWARE datetime and carries +05:30 — strip
    tzinfo before subtracting from a naive `now`.
  * the app holds the DuckDB write lock; copy the file and query the copy.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, time as dtime, timedelta, timezone

ROOT = os.environ.get("OPTIONSLAB_ROOT", "/opt/optionslab")
IST = timezone(timedelta(hours=5, minutes=30))

CORE = ("NIFTY", "BANKNIFTY", "CRUDEOIL", "GOLD")
CHAIN_TABLES = ("chain_snapshots", "option_bars")
STALE_MIN = 15.0          # matches recording_watchdog.STALE_AFTER_S
PROBE_GAP_S = 5.0         # > the app's 3s gate; we are a guest here
RAW_CHARS = 1200          # how much of a raw Dhan response to print

# Every event the chain path can emit, in the order a stall produces them.
CHAIN_EVENT_LIKES = (
    "%chain stalled%", "%chain DEAD%", "%chain STILL dead%",
    "%chain RECOVERED%", "%chain poll%", "%expiry list%",
    "%recorder:%", "%MCX%",
)

RED: list[str] = []


def flag(msg: str) -> None:
    RED.append(msg)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def nse_open(now) -> bool:
    return now.weekday() < 5 and dtime(9, 15) <= now.time() <= dtime(15, 30)


def mcx_open(now) -> bool:
    return now.weekday() < 5 and dtime(9, 0) <= now.time() <= dtime(23, 30)


def session_open(u: str, now) -> bool:
    return mcx_open(now) if u in ("CRUDEOIL", "GOLD") else nse_open(now)


def run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=15).stdout.strip()
    except Exception as e:
        return f"<{e!r}>"


# --- what is on disk --------------------------------------------------------

def show_deploy() -> None:
    section("deploy")
    print(f"  sha:     {run(['git', '-C', ROOT, 'log', '--oneline', '-1'])}")
    print(f"  service: {run(['systemctl', 'is-active', 'optionslab'])}")
    # Is the self-heal actually running, or is this still the old process?
    src = os.path.join(ROOT, "app", "engines", "paper.py")
    try:
        with open(src) as fh:
            has = "_chain_stall_step" in fh.read()
        print(f"  chain self-heal in deployed source: {'YES' if has else 'NO'}")
        if not has:
            flag("deployed source predates the chain self-heal — "
                 "the ladder below will be empty because it isn't running")
    except Exception as e:
        print(f"  (could not read {src}: {e!r})")


def show_recording(now, names) -> None:
    """Per-underlying freshness for BOTH chain tables. diag only looks at
    chain_snapshots; option_bars is written by a different call in the same
    recorder cycle, and seeing them diverge is itself a clue."""
    section("chain recording freshness")
    tmp = tempfile.mkdtemp(prefix="olab-probe-")
    try:
        for f in glob.glob(os.path.join(ROOT, "marketdata.duckdb*")):
            shutil.copy(f, tmp)
        import duckdb
        con = duckdb.connect(os.path.join(tmp, "marketdata.duckdb"))
        for table in CHAIN_TABLES:
            print(f"  {table}:")
            try:
                rows = con.execute(
                    f"SELECT underlying, count(*), max(ts) FROM {table} "   # noqa: S608
                    f"WHERE ts::DATE = current_date GROUP BY 1").fetchall()
            except Exception as e:
                print(f"    (query failed: {e!r})")
                flag(f"could not read {table}: {e!r}")
                continue
            found = {r[0]: r for r in rows}
            for u in names:
                r = found.get(u)
                if not r:
                    state = "NO ROWS TODAY"
                    if session_open(u, now):
                        flag(f"{table}[{u}] has no rows at all today")
                    print(f"    {u:<12} {state}")
                    continue
                age = (now - r[2]).total_seconds() / 60.0
                mark = ""
                if session_open(u, now) and age > STALE_MIN:
                    mark = "  <-- STALE"
                    flag(f"{table}[{u}] stale {age:.0f}m")
                print(f"    {u:<12} rows={r[1]:<7} last={str(r[2])[11:19]} "
                      f"({age:.0f}m ago){mark}")
            # names outside the core list keeping the table warm — this is
            # what hides a dead core underlying behind a fresh-looking table
            others = sorted(set(found) - set(names))
            if others:
                print(f"    (also writing: {', '.join(others[:12])}"
                      f"{' ...' if len(others) > 12 else ''})")
        con.close()
    except Exception as e:
        print(f"  (skipped: {e!r})")
        flag(f"could not read the market-data store: {e!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def show_events(now, limit: int) -> None:
    """The self-heal ladder's own account of itself, plus every chain no-op —
    UNTRUNCATED. The `empty-failure-detail ... raw_response=` line is the
    single most valuable thing in this whole script."""
    section("chain events today (full text, oldest first)")
    day = now.strftime("%Y-%m-%d")
    try:
        db = sqlite3.connect(os.path.join(ROOT, "optionslab.db"))
        where = " OR ".join("message LIKE ?" for _ in CHAIN_EVENT_LIKES)
        rows = db.execute(
            f"SELECT ts, level, message FROM events "                    # noqa: S608
            f"WHERE ts LIKE ? AND ({where}) ORDER BY id DESC LIMIT ?",
            (day + "%", *CHAIN_EVENT_LIKES, limit)).fetchall()
        if not rows:
            print("  (none today)")
            if nse_open(now):
                flag("no chain events at all today — is the poller running?")
        for ts, lvl, msg in reversed(rows):
            print(f"  {ts[11:19]} {lvl:<5} {msg}")
        # A dead chain that escalated should have said so before the push.
        dead = db.execute(
            "SELECT count(*) FROM events WHERE ts LIKE ? "
            "AND message LIKE '%chain DEAD%'", (day + "%",)).fetchone()[0]
        if dead:
            print(f"\n  {dead} 'chain DEAD' escalation(s) today — both remedies "
                  f"failed; the detail is in the lines above")
        db.close()
    except Exception as e:
        print(f"  (skipped: {e!r})")
        flag(f"could not read the events log: {e!r}")


# --- what Dhan says ---------------------------------------------------------

def probe(names, now) -> None:
    """Ask Dhan directly, one name at a time, and print what comes back.

    Deliberately NOT using app.data.dhan_client.fetch_option_chain: that
    unwraps and raises, which is how the payload got discarded in the first
    place. Call the SDK and print the dict."""
    section("live Dhan probe")
    print(f"  spacing requests {PROBE_GAP_S}s apart (the app's own poller is "
          f"also consuming the account's 1-req/3s option-chain budget)")
    try:
        from app.data import dhan_client
        from app.data.dhan_client import MCX_DYNAMIC, UNDERLYINGS
        from app.engines import chain as chainmod
    except Exception as e:
        print(f"  cannot import the app: {e!r}")
        flag("probe could not load app modules")
        return
    if any(u in MCX_DYNAMIC for u in names):
        try:
            ids = dhan_client.resolve_mcx_ids()
            print(f"  MCX ids resolved: {ids}")
        except Exception as e:
            print(f"  MCX id resolution FAILED: {e!r}")
            flag(f"resolve_mcx_ids failed: {e!r}")
    try:
        client = dhan_client.get_client()
    except Exception as e:
        print(f"  cannot build a Dhan client: {e!r}")
        flag(f"get_client failed: {e!r} (token expired?)")
        return

    for u in names:
        print(f"\n  --- {u} ---")
        cfg = UNDERLYINGS.get(u)
        if not cfg:
            print("    NOT IN UNDERLYINGS — nothing would ever be polled for it")
            flag(f"{u} has no UNDERLYINGS entry")
            continue
        sid, seg = cfg.get("security_id"), cfg.get("segment")
        print(f"    sid={sid} seg={seg} session={'open' if session_open(u, now) else 'closed'}")
        try:
            time.sleep(PROBE_GAP_S)
            resp = client.expiry_list(under_security_id=sid,
                                      under_exchange_segment=seg)
            data = resp.get("data") if isinstance(resp, dict) else None
            expiries = data.get("data") if isinstance(data, dict) else data
            expiries = expiries or []
            print(f"    expiry_list: status={resp.get('status')!r} "
                  f"n={len(expiries)} {expiries[:6]}")
            if not expiries:
                print(f"    RAW: {str(resp)[:RAW_CHARS]}")
                flag(f"{u}: expiry_list is EMPTY — every poll is a no-op")
                continue
        except Exception as e:
            print(f"    expiry_list RAISED: {e!r}")
            flag(f"{u}: expiry_list raised {type(e).__name__}")
            continue

        # Exactly what the poller asks for: WEEKLY offset 0 == nearest expiry.
        exp = chainmod.resolve_expiry(expiries, "WEEKLY", 0)
        print(f"    nearest expiry (what the poller requests): {exp}")
        try:
            time.sleep(PROBE_GAP_S)
            resp = client.option_chain(under_security_id=sid,
                                       under_exchange_segment=seg, expiry=exp)
            status = resp.get("status") if isinstance(resp, dict) else None
            print(f"    option_chain: status={status!r}")
            if status == "success":
                payload = (resp.get("data") or {})
                if isinstance(payload.get("data"), dict):
                    payload = payload["data"]
                oc = payload.get("oc") or {}
                print(f"    OK — spot={payload.get('last_price')} "
                      f"strikes={len(oc)}")
                if not oc:
                    flag(f"{u}: chain returned success but ZERO strikes")
            else:
                print(f"    remarks={resp.get('remarks')!r}")
                print(f"    RAW: {str(resp)[:RAW_CHARS]}")
                flag(f"{u}: option_chain FAILED for expiry {exp}")
        except Exception as e:
            print(f"    option_chain RAISED: {e!r}")
            flag(f"{u}: option_chain raised {type(e).__name__}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true",
                    help="also ask Dhan directly (uses the account's API quota)")
    ap.add_argument("--names", default=",".join(CORE),
                    help=f"comma-separated underlyings (default: {','.join(CORE)})")
    ap.add_argument("--events", type=int, default=40,
                    help="how many chain events to print (default 40)")
    args = ap.parse_args()
    names = [n.strip().upper() for n in args.names.split(",") if n.strip()]

    now = datetime.now(IST).replace(tzinfo=None)
    print(f"OptionsLab chain probe — {now:%Y-%m-%d %H:%M:%S} IST  "
          f"(NSE {'OPEN' if nse_open(now) else 'closed'}, "
          f"MCX {'OPEN' if mcx_open(now) else 'closed'})")
    print(f"names: {', '.join(names)}")

    show_deploy()
    show_recording(now, names)
    show_events(now, args.events)
    if args.probe:
        probe(names, now)
    else:
        print("\n(add --probe to ask Dhan what it actually returns for these "
              "names — that is the answer this script exists for)")

    section("VERDICT")
    if RED:
        for r in RED:
            print(f"  RED: {r}")
        return 1
    # "ALL CLEAR" must mean CHECKED AND FINE, never "nothing was readable".
    # A verdict line is the part people act on; an unreadable store printing
    # the same words as a healthy one is worse than no verdict at all.
    print("  ALL CLEAR" + ("" if args.probe else
                           " (read-only — Dhan itself was not asked; "
                           "re-run with --probe if a chain is still dark)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
