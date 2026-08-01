"""
Retroactive trade-blotter P&L backfill
=======================================
Stamp `net_pnl`/`gross_pnl` onto historical EXIT rows in the `trades` table
that predate the History-view P&L columns (they were only ever computed for
NEWLY recorded exits going forward — see PaperContext._blotter,
LiveContext._blotter, ScannerTrader._book_trade, and the manual-trade
endpoint). Existing rows have no such field, so History's "Show trades"
drill-down shows a dash for anything that closed before that change shipped.

Pairing strategy: each blotter row already carries `reason` ("entry" for the
opening leg, anything else for the closing leg), `contract`, and `tag`. Group
rows by (strategy_id, contract, tag), sort chronologically, and walk them
FIFO — the oldest unmatched entry pairs with the next exit in that group.
This mirrors how Position.realized_pnl is computed live: gross = (exit_price
- entry_price) * signed_qty; net = gross - (entry_fees + exit_fees).

SAFE BY CONSTRUCTION:
  * only ever adds the two new keys to an exit row's payload_json — prices,
    fees, everything else is untouched
  * rows that already carry net_pnl (booked after the fix shipped) are left
    alone, so re-running is harmless
  * an exit with no unmatched entry in its group is REPORTED, not guessed at

Run ON THE VPS (writes optionslab.db directly, not through the API):
    venv/bin/python -m scripts.backfill_trade_pnl --dry-run
    venv/bin/python -m scripts.backfill_trade_pnl
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict, deque

from app.core.registry import _conn


def _pair_group(rows: list[tuple[str, dict]]) -> tuple[list[tuple[str, dict]], int]:
    """rows: [(trade_id, payload), ...] for one (strategy_id, contract, tag)
    group, any order. Returns (rows to patch, count of unmatched exits)."""
    rows = sorted(rows, key=lambda r: r[1].get("ts", ""))
    pending: deque[tuple[str, dict]] = deque()
    to_patch = []
    unmatched = 0
    for tid, p in rows:
        if p.get("reason") == "entry":
            pending.append((tid, p))
            continue
        if "net_pnl" in p:
            if pending:
                pending.popleft()   # keep FIFO order correct for later legs
            continue
        if not pending:
            unmatched += 1
            continue
        _entry_tid, entry = pending.popleft()
        entry_price, exit_price = entry.get("price"), p.get("price")
        qty = p.get("qty") or entry.get("qty")
        if entry_price is None or exit_price is None or not qty:
            unmatched += 1
            continue
        signed_qty = qty if entry.get("side") == "BUY" else -qty
        gross = (exit_price - entry_price) * signed_qty
        net = gross - (entry.get("fees", 0.0) + p.get("fees", 0.0))
        p["gross_pnl"] = round(gross, 2)
        p["net_pnl"] = round(net, 2)
        to_patch.append((tid, p))
    return to_patch, unmatched


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, write nothing")
    args = ap.parse_args()

    with _conn() as c:
        rows = c.execute("SELECT id, strategy_id, payload_json FROM trades").fetchall()

    groups: dict[tuple, list] = defaultdict(list)
    skipped_bad_json = 0
    for r in rows:
        try:
            p = json.loads(r["payload_json"])
        except (TypeError, ValueError):
            skipped_bad_json += 1
            continue
        key = (r["strategy_id"], p.get("contract", ""), p.get("tag", ""))
        groups[key].append((r["id"], p))

    to_patch = []
    unmatched_total = 0
    for key, items in groups.items():
        patched, unmatched = _pair_group(items)
        to_patch.extend(patched)
        unmatched_total += unmatched

    print(f"trade rows scanned: {len(rows)} ({skipped_bad_json} unparsable, skipped)")
    print(f"groups (strategy/contract/tag): {len(groups)}")
    print(f"exit rows to backfill: {len(to_patch)}")
    print(f"exit rows with no matching entry (left untouched): {unmatched_total}")

    if args.dry_run:
        print("dry-run: nothing written")
        return 0

    if to_patch:
        with _conn() as c:
            c.executemany(
                "UPDATE trades SET payload_json=? WHERE id=?",
                [(json.dumps(p), tid) for tid, p in to_patch])
            c.commit()
    print(f"done: {len(to_patch)} row(s) updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
