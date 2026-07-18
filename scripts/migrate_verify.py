#!/usr/bin/env python3
"""Migration fingerprint + compare for the two OptionsLab databases.

The whole app state is two local files — `optionslab.db` (SQLite: strategies,
trades, daily P&L, settings, …) and `marketdata.duckdb` (DuckDB: recorded bars
and chain snapshots). When moving the box (see deploy/MIGRATE.md), you copy
both across and need to *prove* the copy is complete before retiring the old
box. This script computes a fingerprint (row counts + per-underlying coverage +
date ranges) and can diff two fingerprints across machines.

READ-ONLY. Opens the DuckDB file read-only, so stop the app first (single
writer) to get a consistent snapshot, but this script never writes to the DBs.

Usage:
  # on the OLD box, after `systemctl stop optionslab`:
  venv/bin/python scripts/migrate_verify.py --save /tmp/old.json

  # on the NEW box, after copying both DB files across:
  venv/bin/python scripts/migrate_verify.py --compare /tmp/old.json
  #   -> prints MATCH and exits 0 when every metric lines up, else MISMATCH / exit 1

  # just look at one box:
  venv/bin/python scripts/migrate_verify.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# tables we expect in each store (missing ones are reported as absent, not 0,
# so a schema drift shows up instead of hiding as an empty table)
REGISTRY_TABLES = ["strategies", "trades", "daily_pnl", "events", "settings",
                   "paper_state", "backfill_chunks", "backtest_runs", "dhan_token"]
MARKET_TABLES = ["underlying_bars", "option_bars", "chain_snapshots",
                 "stock_snapshots", "fno_universe", "setup_flags",
                 "index_bias_history", "index_bias_accuracy"]


def _sqlite_fp(path: Path) -> dict:
    fp: dict = {}
    if not path.exists():
        return {"_missing": True}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        have = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in REGISTRY_TABLES:
            fp[f"registry.{t}"] = (con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                                   if t in have else "ABSENT")
        # daily_pnl split by mode — PAPER and LIVE ledgers must never be conflated
        if "daily_pnl" in have:
            for mode in ("PAPER", "LIVE"):
                n = con.execute("SELECT count(*) FROM daily_pnl WHERE mode=?",
                                (mode,)).fetchone()[0]
                fp[f"registry.daily_pnl.{mode}"] = n
    finally:
        con.close()
    return fp


def _duck_fp(path: Path) -> dict:
    fp: dict = {}
    if not path.exists():
        return {"_missing": True}
    try:
        import duckdb
    except ImportError:
        return {"_error": "duckdb not importable"}
    try:
        con = duckdb.connect(str(path), read_only=True)
    except Exception as e:
        return {"_error": f"open failed: {e!r}"}
    try:
        have = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables").fetchall()}
        for t in MARKET_TABLES:
            fp[f"market.{t}"] = (con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                                 if t in have else "ABSENT")
        # per-underlying coverage + date span for the two big recorded tables
        for t in ("underlying_bars", "option_bars"):
            if t not in have:
                continue
            for u, n, lo, hi in con.execute(
                    f'SELECT underlying, count(*), min(ts), max(ts) '
                    f'FROM "{t}" GROUP BY underlying ORDER BY underlying').fetchall():
                fp[f"coverage.{t}.{u}.rows"] = n
                fp[f"coverage.{t}.{u}.min_ts"] = str(lo)
                fp[f"coverage.{t}.{u}.max_ts"] = str(hi)
        if "chain_snapshots" in have:
            lo, hi = con.execute(
                "SELECT min(ts), max(ts) FROM chain_snapshots").fetchone()
            fp["chain_snapshots.min_ts"] = str(lo)
            fp["chain_snapshots.max_ts"] = str(hi)
    finally:
        con.close()
    return fp


def fingerprint(reg_path: Path, mkt_path: Path) -> dict:
    fp = {"registry_file": str(reg_path), "market_file": str(mkt_path)}
    fp.update(_sqlite_fp(reg_path))
    fp.update(_duck_fp(mkt_path))
    return fp


def _print_fp(fp: dict) -> None:
    for k in sorted(fp):
        if k in ("registry_file", "market_file"):
            continue
        print(f"  {k:48} {fp[k]}")


def compare(cur: dict, other: dict) -> int:
    """Diff two fingerprints on their data metrics (ignoring file-path keys).
    Returns 0 if every shared metric matches and neither side has extra
    metrics, else 1."""
    skip = {"registry_file", "market_file"}
    keys = (set(cur) | set(other)) - skip
    mismatches = []
    for k in sorted(keys):
        a, b = cur.get(k, "<absent-new>"), other.get(k, "<absent-old>")
        if a != b:
            mismatches.append((k, b, a))   # (metric, old, new)
    if not mismatches:
        print(f"MATCH — all {len(keys)} metrics identical across both boxes.")
        return 0
    print(f"MISMATCH — {len(mismatches)} of {len(keys)} metrics differ "
          f"(metric: old -> new):", file=sys.stderr)
    for k, old, new in mismatches:
        print(f"  {k:48} {old}  ->  {new}", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fingerprint/compare the OptionsLab DBs for migration")
    ap.add_argument("--registry", default=str(ROOT / "optionslab.db"),
                    help="path to optionslab.db (SQLite)")
    ap.add_argument("--market", default=str(ROOT / "marketdata.duckdb"),
                    help="path to marketdata.duckdb (DuckDB)")
    ap.add_argument("--save", default="", help="write this box's fingerprint JSON here")
    ap.add_argument("--compare", default="",
                    help="compare this box against a fingerprint JSON saved on the other box")
    args = ap.parse_args(argv)

    fp = fingerprint(Path(args.registry), Path(args.market))
    print(f"Fingerprint  registry={args.registry}  market={args.market}", file=sys.stderr)
    _print_fp(fp)

    if args.save:
        Path(args.save).write_text(json.dumps(fp, indent=2, default=str), encoding="utf-8")
        print(f"[saved to {args.save}]", file=sys.stderr)

    if args.compare:
        other = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        print(file=sys.stderr)
        return compare(fp, other)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
