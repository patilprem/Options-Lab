"""
Strategy Registry & Lifecycle
=============================
Every pasted strategy becomes an *instance* with its own id, code,
allocated capital and lifecycle state:

    DRAFT -> VALIDATED -> (backtests any number of times)
                        -> DEPLOYED_PAUSED <-> RUNNING -> STOPPED

Play/Pause semantics (important for options!):
  * PAUSE  = engine stops accepting NEW entries from the strategy, but by
             default keeps managing EXITS of open positions (stop-losses
             keep working). `square_off_on_pause=True` flattens instead.
  * PLAY   = entries allowed again.
  * STOP   = square off everything, strategy unloaded.

Capital allocation:
  * `allocated_capital` is virtual money reserved for this instance.
  * Engines block entries whose estimated margin > available capital.
"""

from __future__ import annotations

import enum
import json
import sqlite3
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parents[2] / "optionslab.db"


class State(str, enum.Enum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    DEPLOYED_PAUSED = "DEPLOYED_PAUSED"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"


ALLOWED_TRANSITIONS = {
    State.DRAFT: {State.VALIDATED},
    State.VALIDATED: {State.DEPLOYED_PAUSED, State.DRAFT},
    State.DEPLOYED_PAUSED: {State.RUNNING, State.STOPPED},
    State.RUNNING: {State.DEPLOYED_PAUSED, State.STOPPED},
    State.STOPPED: {State.DEPLOYED_PAUSED},  # allow redeploy
}


@dataclass
class StrategyRecord:
    id: str
    name: str
    code: str
    state: State
    allocated_capital: float
    square_off_on_pause: bool
    meta_json: str
    created_at: str
    updated_at: str

    @property
    def meta(self) -> dict:
        return json.loads(self.meta_json or "{}")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS strategies (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            code TEXT NOT NULL,
            state TEXT NOT NULL,
            allocated_capital REAL NOT NULL DEFAULT 0,
            square_off_on_pause INTEGER NOT NULL DEFAULT 0,
            meta_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS backtest_runs (
            id TEXT PRIMARY KEY,
            strategy_id TEXT NOT NULL REFERENCES strategies(id),
            started_at TEXT, finished_at TEXT,
            from_date TEXT, to_date TEXT,
            result_json TEXT
        );
        CREATE TABLE IF NOT EXISTS daily_pnl (
            strategy_id TEXT NOT NULL REFERENCES strategies(id),
            mode TEXT NOT NULL DEFAULT 'PAPER',   -- PAPER | LIVE
            trade_date TEXT NOT NULL,
            realized REAL, unrealized REAL, fees REAL,
            equity_eod REAL,
            PRIMARY KEY (strategy_id, mode, trade_date)
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            strategy_id TEXT,                  -- NULL for system events
            level TEXT NOT NULL,               -- info | warn | error
            kind TEXT NOT NULL,                -- fill|block|stop_loss|engine|feed|token|lifecycle
            message TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT
        );
        CREATE TABLE IF NOT EXISTS trades (
            id TEXT PRIMARY KEY,
            strategy_id TEXT NOT NULL,
            mode TEXT NOT NULL,               -- 'BACKTEST' | 'PAPER'
            run_id TEXT,
            payload_json TEXT NOT NULL         -- serialized Position
        );
        CREATE TABLE IF NOT EXISTS paper_state (
            strategy_id TEXT PRIMARY KEY REFERENCES strategies(id),
            snapshot_json TEXT NOT NULL,       -- {date, margin_used, realized_today, fees_today, positions[]}
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS backfill_chunks (
            key TEXT PRIMARY KEY,              -- underlying|kind|interval|off|type|ekind|eoff|from|to
            done_at TEXT NOT NULL
        );
        """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create(name: str, code: str) -> StrategyRecord:
    rec = StrategyRecord(
        id=str(uuid.uuid4())[:8], name=name, code=code, state=State.DRAFT,
        allocated_capital=0.0, square_off_on_pause=False, meta_json="{}",
        created_at=_now(), updated_at=_now(),
    )
    with _conn() as c:
        c.execute(
            "INSERT INTO strategies VALUES (?,?,?,?,?,?,?,?,?)",
            (rec.id, rec.name, rec.code, rec.state.value, rec.allocated_capital,
             int(rec.square_off_on_pause), rec.meta_json, rec.created_at, rec.updated_at),
        )
    return rec


def get(strategy_id: str) -> Optional[StrategyRecord]:
    with _conn() as c:
        row = c.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["state"] = State(d["state"])
    d["square_off_on_pause"] = bool(d["square_off_on_pause"])
    return StrategyRecord(**d)


def list_all() -> list[StrategyRecord]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM strategies ORDER BY created_at DESC").fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["state"] = State(d["state"])
        d["square_off_on_pause"] = bool(d["square_off_on_pause"])
        out.append(StrategyRecord(**d))
    return out


def transition(strategy_id: str, new_state: State) -> StrategyRecord:
    rec = get(strategy_id)
    if rec is None:
        raise KeyError(f"No strategy {strategy_id}")
    if new_state not in ALLOWED_TRANSITIONS[rec.state]:
        raise ValueError(f"Illegal transition {rec.state.value} -> {new_state.value}")
    with _conn() as c:
        c.execute("UPDATE strategies SET state=?, updated_at=? WHERE id=?",
                  (new_state.value, _now(), strategy_id))
    return get(strategy_id)


def set_meta(strategy_id: str, meta: dict) -> None:
    with _conn() as c:
        c.execute("UPDATE strategies SET meta_json=?, updated_at=? WHERE id=?",
                  (json.dumps(meta), _now(), strategy_id))


def rename(strategy_id: str, name: str) -> StrategyRecord:
    name = name.strip()
    if not name:
        raise ValueError("name cannot be empty")
    rec = get(strategy_id)
    if rec is None:
        raise KeyError(strategy_id)
    with _conn() as c:
        c.execute("UPDATE strategies SET name=?, updated_at=? WHERE id=?",
                  (name, _now(), strategy_id))
    return get(strategy_id)


def update_code(strategy_id: str, code: str) -> StrategyRecord:
    """Replace a strategy's code. Caller must re-validate + set_meta +
    transition to VALIDATED afterwards; this only guards state and resets
    to DRAFT since the old meta/validation no longer applies."""
    rec = get(strategy_id)
    if rec is None:
        raise KeyError(strategy_id)
    if rec.state in (State.RUNNING, State.DEPLOYED_PAUSED):
        raise ValueError("Stop the strategy before editing its code")
    with _conn() as c:
        c.execute("UPDATE strategies SET code=?, state=?, updated_at=? WHERE id=?",
                  (code, State.DRAFT.value, _now(), strategy_id))
    return get(strategy_id)


def delete(strategy_id: str) -> None:
    rec = get(strategy_id)
    if rec is None:
        raise KeyError(strategy_id)
    if rec.state in (State.RUNNING, State.DEPLOYED_PAUSED):
        raise ValueError("Stop the strategy before deleting it")
    with _conn() as c:
        c.execute("DELETE FROM strategies WHERE id=?", (strategy_id,))
        c.execute("DELETE FROM backtest_runs WHERE strategy_id=?", (strategy_id,))
        c.execute("DELETE FROM daily_pnl WHERE strategy_id=?", (strategy_id,))
        c.execute("DELETE FROM events WHERE strategy_id=?", (strategy_id,))
        c.execute("DELETE FROM trades WHERE strategy_id=?", (strategy_id,))
        c.execute("DELETE FROM paper_state WHERE strategy_id=?", (strategy_id,))
        c.execute("DELETE FROM settings WHERE key LIKE ?", (f"%:{strategy_id}",))


def allocate(strategy_id: str, capital: float, square_off_on_pause: Optional[bool] = None) -> StrategyRecord:
    if capital < 0:
        raise ValueError("capital must be >= 0")
    rec = get(strategy_id)
    if rec is None:
        raise KeyError(strategy_id)
    if rec.state == State.RUNNING and capital < rec.allocated_capital:
        raise ValueError("Pause the strategy before reducing its capital")
    with _conn() as c:
        c.execute("UPDATE strategies SET allocated_capital=?, updated_at=? WHERE id=?",
                  (capital, _now(), strategy_id))
        if square_off_on_pause is not None:
            c.execute("UPDATE strategies SET square_off_on_pause=? WHERE id=?",
                      (int(square_off_on_pause), strategy_id))
    return get(strategy_id)


def save_backtest(strategy_id: str, run_id: str, from_date: str, to_date: str, result: dict) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO backtest_runs VALUES (?,?,?,?,?,?,?)",
                  (run_id, strategy_id, _now(), _now(), from_date, to_date, json.dumps(result)))


def save_day(strategy_id: str, mode: str, trade_date: str, realized: float,
             unrealized: float, fees: float, equity_eod: float) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO daily_pnl VALUES (?,?,?,?,?,?,?)",
                  (strategy_id, mode.upper(), trade_date, realized, unrealized,
                   fees, equity_eod))


def save_paper_day(strategy_id: str, trade_date: str, realized: float,
                   unrealized: float, fees: float, equity_eod: float) -> None:
    save_day(strategy_id, "PAPER", trade_date, realized, unrealized, fees, equity_eod)


def prev_equity(strategy_id: str, before_date: str,
                mode: str = "PAPER") -> Optional[float]:
    """Last equity_eod strictly before `before_date` (None on day 1)."""
    with _conn() as c:
        row = c.execute(
            "SELECT equity_eod FROM daily_pnl WHERE strategy_id=? AND mode=? "
            "AND trade_date<? ORDER BY trade_date DESC LIMIT 1",
            (strategy_id, mode.upper(), before_date)).fetchone()
    return row[0] if row and row[0] is not None else None


def cum_pnl(strategy_id: str, mode: str = "PAPER") -> float:
    """Lifetime realized P&L (net of fees) across all daily rows."""
    with _conn() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(realized), 0) FROM daily_pnl "
            "WHERE strategy_id=? AND mode=?",
            (strategy_id, mode.upper())).fetchone()
    return row[0] or 0.0


def performance_rows(strategy_id: str, mode: str = "PAPER") -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM daily_pnl WHERE strategy_id=? AND mode=? ORDER BY trade_date",
            (strategy_id, mode.upper())).fetchall()
    return [dict(r) for r in rows]


def record_event(level: str, kind: str, message: str,
                 strategy_id: str | None = None) -> None:
    ts = datetime.now(timezone(timedelta(hours=5, minutes=30))).isoformat(sep=" ", timespec="seconds")
    with _conn() as c:
        c.execute("INSERT INTO events (ts, strategy_id, level, kind, message) VALUES (?,?,?,?,?)",
                  (ts, strategy_id, level, kind, message))


def events_for(day: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT e.ts, e.strategy_id, s.name AS strategy, e.level, e.kind, e.message "
            "FROM events e LEFT JOIN strategies s ON s.id = e.strategy_id "
            "WHERE e.ts LIKE ? ORDER BY e.ts DESC", (day + "%",)).fetchall()
    return [dict(r) for r in rows]


def setting(key: str, default: str = "") -> str:
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))


def chunk_key(underlying: str, kind: str, interval: int, from_d, to_d,
              off: int = 0, opt: str = "", expiry_kind: str = "",
              expiry_offset: int = 0) -> str:
    return f"{underlying}|{kind}|{interval}|{off}|{opt}|{expiry_kind}|{expiry_offset}|{from_d}|{to_d}"


def is_chunk_done(key: str) -> bool:
    """A chunk is 'done' only after its rows were successfully upserted. This
    (not row-existence) is the safe resume signal — a partially-written or
    concurrently-clobbered range never looks complete."""
    with _conn() as c:
        return c.execute("SELECT 1 FROM backfill_chunks WHERE key=?", (key,)).fetchone() is not None


def mark_chunk_done(key: str) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO backfill_chunks VALUES (?,?)", (key, _now()))


def clear_chunks(underlying: str = "") -> int:
    with _conn() as c:
        if underlying:
            cur = c.execute("DELETE FROM backfill_chunks WHERE key LIKE ?", (underlying + "|%",))
        else:
            cur = c.execute("DELETE FROM backfill_chunks")
        return cur.rowcount


def count_chunks(underlying: str = "") -> int:
    with _conn() as c:
        if underlying:
            return c.execute("SELECT count(*) FROM backfill_chunks WHERE key LIKE ?",
                             (underlying + "|%",)).fetchone()[0]
        return c.execute("SELECT count(*) FROM backfill_chunks").fetchone()[0]


def set_backfill_status(status: dict) -> None:
    """Persist backfill progress to SQLite so ANY process (in-app task or a
    background CLI job) is visible to the server/UI. SQLite is multi-process
    safe (unlike DuckDB's single writer)."""
    set_setting("backfill_status", json.dumps(status))


def get_backfill_status() -> dict:
    raw = setting("backfill_status", "")
    return json.loads(raw) if raw else {"running": False, "message": "idle",
                                         "done": 0, "total": 0}


def set_params(strategy_id: str, params: dict) -> None:
    set_setting(f"params:{strategy_id}", json.dumps(params))


def get_params(strategy_id: str) -> dict:
    raw = setting(f"params:{strategy_id}", "")
    return json.loads(raw) if raw else {}


def all_trades(from_date: str = "", to_date: str = "",
               strategy_id: str = "", mode: str = "") -> list[dict]:
    q = ("SELECT t.strategy_id, s.name AS strategy, t.mode, t.payload_json "
         "FROM trades t LEFT JOIN strategies s ON s.id = t.strategy_id WHERE 1=1")
    args: list = []
    if strategy_id:
        q += " AND t.strategy_id=?"; args.append(strategy_id)
    if mode:
        q += " AND t.mode=?"; args.append(mode.upper())
    with _conn() as c:
        rows = c.execute(q, args).fetchall()
    out = []
    for r in rows:
        t = json.loads(r["payload_json"])
        d = str(t.get("ts", ""))[:10]
        if from_date and d < from_date:
            continue
        if to_date and d > to_date:
            continue
        out.append({**t, "strategy": r["strategy"], "strategy_id": r["strategy_id"],
                    "mode": r["mode"]})
    return sorted(out, key=lambda t: t.get("ts", ""), reverse=True)


def save_paper_state(strategy_id: str, snapshot: dict) -> None:
    """Persist a paper strategy's live session snapshot (open positions +
    margin/P&L) so a process restart can recover it. One row per strategy."""
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO paper_state VALUES (?,?,?)",
                  (strategy_id, json.dumps(snapshot), _now()))


def load_paper_state(strategy_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT snapshot_json FROM paper_state WHERE strategy_id=?",
                        (strategy_id,)).fetchone()
    return json.loads(row[0]) if row else None


def clear_paper_state(strategy_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM paper_state WHERE strategy_id=?", (strategy_id,))


def record_trade(strategy_id: str, mode: str, payload: dict, run_id: str = "") -> None:
    """One blotter row per fill (entries and exits both). payload keys:
    ts, contract, side, qty, price, fees, margin, reason, tag."""
    with _conn() as c:
        c.execute("INSERT INTO trades VALUES (?,?,?,?,?)",
                  (str(uuid.uuid4()), strategy_id, mode, run_id, json.dumps(payload)))


def purge_phantom_days() -> int:
    """Delete all-zero daily_pnl rows stamped on weekends — artifacts of the
    pre-guard EOD clock firing on closed days. Never touches rows with any
    real P&L/fees (safety against deleting a legitimately flat session)."""
    with _conn() as c:
        cur = c.execute(
            """DELETE FROM daily_pnl
               WHERE strftime('%w', trade_date) IN ('0', '6')
                 AND ifnull(realized, 0) = 0 AND ifnull(unrealized, 0) = 0
                 AND ifnull(fees, 0) = 0""")
        return cur.rowcount


def wipe_paper_day(strategy_id: str, day: str) -> dict:
    """Remove one strategy's PAPER records for one day (trades + daily row +
    state snapshot) — incident cleanup for fills produced by bad quotes
    (e.g. the frozen-chain fills of 2026-07-13). LIVE rows are never touched."""
    with _conn() as c:
        t = c.execute("""DELETE FROM trades WHERE strategy_id=? AND mode='PAPER'
                         AND payload_json LIKE ?""", (strategy_id, f'%"{day}%')).rowcount
        d = c.execute("DELETE FROM daily_pnl WHERE strategy_id=? AND mode='PAPER' "
                      "AND trade_date=?", (strategy_id, day)).rowcount
        s = c.execute("DELETE FROM paper_state WHERE strategy_id=?",
                      (strategy_id,)).rowcount
    recompute_equity_chain(strategy_id, "PAPER", day)
    return {"trades_deleted": t, "daily_rows_deleted": d, "state_cleared": s}


def recompute_equity_chain(strategy_id: str, mode: str = "PAPER",
                           from_date: Optional[str] = None) -> int:
    """Rebuild equity_eod for every daily_pnl row on/after `from_date` (the
    whole ledger when omitted) by re-chaining stored realized+unrealized day
    over day from the last untouched close (or allocated_capital on day 1).

    wipe_paper_day/manual_trade only rewrite the one day they touch; any
    later row was chained off that day's *old* equity_eod and goes stale.
    Call this after either to keep the curve consistent, or directly as a
    one-off repair for drift left over from before this existed."""
    with _conn() as c:
        cap_row = c.execute("SELECT allocated_capital FROM strategies WHERE id=?",
                            (strategy_id,)).fetchone()
        cap = cap_row[0] if cap_row else 0.0
        if from_date is None:
            base, start = cap, ""
        else:
            prev = c.execute(
                "SELECT equity_eod FROM daily_pnl WHERE strategy_id=? AND mode=? "
                "AND trade_date<? ORDER BY trade_date DESC LIMIT 1",
                (strategy_id, mode.upper(), from_date)).fetchone()
            base = prev[0] if prev and prev[0] is not None else cap
            start = from_date
        rows = c.execute(
            "SELECT trade_date, realized, unrealized FROM daily_pnl "
            "WHERE strategy_id=? AND mode=? AND trade_date>=? ORDER BY trade_date",
            (strategy_id, mode.upper(), start)).fetchall()
        for trade_date, realized, unrealized in rows:
            base = round(base + (realized or 0.0) + (unrealized or 0.0), 2)
            c.execute("UPDATE daily_pnl SET equity_eod=? WHERE strategy_id=? "
                      "AND mode=? AND trade_date=?",
                      (base, strategy_id, mode.upper(), trade_date))
        return len(rows)


def count_trades(mode: str = "PAPER") -> int:
    """Total blotter fills for a ledger (experience counter for /data/maturity)."""
    with _conn() as c:
        return c.execute("SELECT count(*) FROM trades WHERE mode=?",
                         (mode,)).fetchone()[0]


def trades_for(strategy_id: str, trade_date: str, mode: str = "PAPER") -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT payload_json FROM trades WHERE strategy_id=? AND mode=?",
                         (strategy_id, mode)).fetchall()
    out = [json.loads(r[0]) for r in rows]
    out = [t for t in out if str(t.get("ts", "")).startswith(trade_date)]
    return sorted(out, key=lambda t: t.get("ts", ""))


def paper_performance(strategy_id: str) -> list[dict]:
    return performance_rows(strategy_id, "PAPER")


def backtests(strategy_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, from_date, to_date, finished_at, result_json FROM backtest_runs "
            "WHERE strategy_id=? ORDER BY finished_at DESC", (strategy_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["result"] = json.loads(d.pop("result_json") or "{}")
        out.append(d)
    return out
