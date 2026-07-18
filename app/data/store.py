"""
Market Data Store
=================
DuckDB-backed storage for:
  * underlying_bars  — spot/futures candles (from Dhan intraday historical)
  * option_bars      — ATM-relative option candles (from Dhan
                       expired_options_data): minute OHLC + IV + OI,
                       keyed by (underlying, ts, expiry_kind, expiry_offset,
                       strike_offset, option_type)

The backtest engine ONLY reads from this store — never from the API —
so backtests are fast, free, and reproducible. Run the downloader
(app/data/dhan_client.py) to populate it.

If the store is empty, `SyntheticStore` generates a plausible random-walk
market so you can develop and test the whole platform before wiring Dhan.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, date, timedelta, time
from pathlib import Path

from app.core.contract import Bar, OptionQuote, OptionType, LegSpec

DB_PATH = Path(__file__).resolve().parents[2] / "marketdata.duckdb"

SCHEMA = """
CREATE TABLE IF NOT EXISTS underlying_bars (
    underlying VARCHAR, ts TIMESTAMP,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    volume DOUBLE, oi DOUBLE,
    PRIMARY KEY (underlying, ts)
);
CREATE TABLE IF NOT EXISTS option_bars (
    underlying VARCHAR, ts TIMESTAMP,
    expiry_kind VARCHAR, expiry_offset INTEGER,
    strike_offset INTEGER, option_type VARCHAR,
    strike DOUBLE, expiry DATE,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    volume DOUBLE, oi DOUBLE, iv DOUBLE,
    PRIMARY KEY (underlying, ts, expiry_kind, expiry_offset, strike_offset, option_type)
);
-- Backtest hot path: option_close() fixes the (underlying, kind, offsets, type)
-- tuple and ranges on ts (ORDER BY ts DESC LIMIT 1). The PK leads with ts, so it
-- can't serve that lookup — it scans. This index leads with the equality columns
-- and puts ts last, turning each mark-to-market into a seek instead of a scan.
CREATE INDEX IF NOT EXISTS idx_option_lookup ON option_bars
    (underlying, expiry_kind, expiry_offset, strike_offset, option_type, ts);
-- Full-fidelity live chain recording (institutional-footprint research):
-- everything the option-chain API returns, per strike per poll — bid/ask,
-- IV, OI, volume and greeks that option_bars' OHLC shape can't hold.
CREATE TABLE IF NOT EXISTS chain_snapshots (
    underlying VARCHAR, ts TIMESTAMP,
    expiry DATE, expiry_kind VARCHAR, expiry_offset INTEGER,
    strike DOUBLE, strike_offset INTEGER, option_type VARCHAR,
    spot DOUBLE,
    ltp DOUBLE, bid DOUBLE, ask DOUBLE,
    iv DOUBLE, oi DOUBLE, volume DOUBLE,
    delta DOUBLE, theta DOUBLE, vega DOUBLE, gamma DOUBLE,
    PRIMARY KEY (underlying, ts, expiry_kind, expiry_offset, strike, option_type)
);
-- FNO stock universe (scanner F1): a DATED snapshot of every NSE FNO stock's
-- ids/lot/expiries, resolved from the scrip master. Dated so lot-size history
-- accumulates the way backtest.LOT_HISTORY does — a symbol's lot changes by
-- exchange circular, and old backtests must see the lot that applied then.
CREATE TABLE IF NOT EXISTS fno_universe (
    as_of DATE, symbol VARCHAR,
    spot_security_id INTEGER, future_security_id INTEGER,
    fno_segment VARCHAR, lot_size INTEGER,
    near_expiry DATE, expiries VARCHAR,
    PRIMARY KEY (as_of, symbol)
);
-- Tier-1 scanner snapshots (F2): one batched market-quote row per FNO stock
-- per poll — future LTP + day OHLC + cumulative volume + futures OI, and the
-- cash-equity spot. Buildup/volume-surge are derived from consecutive rows;
-- volume baselines from the trailing-N-day rows at the same time-of-day.
CREATE TABLE IF NOT EXISTS stock_snapshots (
    symbol VARCHAR, ts TIMESTAMP,
    spot DOUBLE, fut_ltp DOUBLE,
    day_open DOUBLE, day_high DOUBLE, day_low DOUBLE, prev_close DOUBLE,
    volume DOUBLE, oi DOUBLE,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_stock_snap ON stock_snapshots (symbol, ts);
-- Index bias (F5): a recorded time series of the constituent-weighted breadth
-- read for NIFTY/BANKNIFTY, with its inputs, so the signal's accuracy vs the
-- realized index move can be measured from day one (never trusted un-tested).
CREATE TABLE IF NOT EXISTS index_bias_history (
    ts TIMESTAMP, index_name VARCHAR,
    score DOUBLE, buildup_breadth DOUBLE, price_breadth DOUBLE,
    bull_weight DOUBLE, bear_weight DOUBLE, coverage DOUBLE, n INTEGER,
    spot DOUBLE,
    PRIMARY KEY (ts, index_name)
);
CREATE TABLE IF NOT EXISTS index_bias_accuracy (
    day DATE, index_name VARCHAR, horizon_min INTEGER,
    n INTEGER, hits INTEGER, hit_rate DOUBLE, avg_forward_move DOUBLE,
    PRIMARY KEY (day, index_name, horizon_min)
);
-- Setup flags (F6 validation): every above-threshold scored setup, with the
-- bias-side ATM premium at flag time, so forward option returns (did buying it
-- pay?) can be measured against later chain_snapshots. This is how the scanner
-- earns trust BEFORE any capital — measured hit-rate, not a faked backtest.
CREATE TABLE IF NOT EXISTS setup_flags (
    ts TIMESTAMP, symbol VARCHAR,
    bias VARCHAR, score DOUBLE, spot DOUBLE, atm_premium DOUBLE,
    PRIMARY KEY (ts, symbol)
);
"""


class DataStore:
    """Real store. Requires `pip install duckdb` and downloaded data.

    A single DuckDB connection is NOT safe for concurrent use — FastAPI runs
    sync endpoints in a threadpool, so a backtest and a dashboard poll can hit
    it at once (that returned None from a COUNT and 500'd). All reads go through
    `_q` / `_q1` under a lock so access is serialised."""

    def __init__(self, path: Path = DB_PATH):
        import duckdb
        import threading
        self.con = duckdb.connect(str(path))
        self.con.execute(SCHEMA)
        self._lock = threading.Lock()

    def _q(self, sql: str, params=None):
        with self._lock:
            return self.con.execute(sql, params or []).fetchall()

    def _q1(self, sql: str, params=None):
        with self._lock:
            return self.con.execute(sql, params or []).fetchone()

    def underlying_bars(self, underlying: str, start: datetime, end: datetime,
                        interval_min: int) -> list[Bar]:
        rows = self._q(
            """SELECT time_bucket(INTERVAL (? || ' minutes'), ts) AS bts,
                      first(open ORDER BY ts), max(high), min(low),
                      last(close ORDER BY ts), sum(volume), last(oi ORDER BY ts)
               FROM underlying_bars
               WHERE underlying=? AND ts BETWEEN ? AND ?
               GROUP BY bts ORDER BY bts""",
            [str(interval_min), underlying, start, end])
        return [Bar(r[0], r[1], r[2], r[3], r[4], r[5] or 0, r[6] or 0) for r in rows]

    def option_close(self, underlying: str, ts: datetime, leg: LegSpec):
        row = self._q1(
            """SELECT close, strike, expiry, iv, oi FROM option_bars
               WHERE underlying=? AND expiry_kind=? AND expiry_offset=?
                 AND strike_offset=? AND option_type=? AND ts <= ?
               ORDER BY ts DESC LIMIT 1""",
            [underlying, leg.expiry_kind.value, leg.expiry_offset,
             leg.strike_offset, leg.option_type.value, ts])
        if not row:
            return None
        return OptionQuote(ts, underlying, row[2], row[1], leg.option_type,
                           ltp=row[0], iv=row[3], oi=row[4])

    def option_series(self, underlying: str, expiry_kind: str, expiry_offset: int,
                      strike_offset: int, option_type: str, end: datetime):
        """All bars for one option key up to `end`, ascending — for the backtest
        to preload once and bisect in-memory instead of a query per bar.
        Returns rows of (ts, close, strike, expiry, iv, oi)."""
        return self._q(
            """SELECT ts, close, strike, expiry, iv, oi FROM option_bars
               WHERE underlying=? AND expiry_kind=? AND expiry_offset=?
                 AND strike_offset=? AND option_type=? AND ts <= ?
               ORDER BY ts""",
            [underlying, expiry_kind, expiry_offset, strike_offset, option_type, end])

    def option_series_by_strike(self, underlying: str, strike: float,
                                option_type: str, expiry_kind: str,
                                expiry_offset: int, end: datetime):
        """All bars for one CONTRACT (fixed strike) up to `end`. The
        ATM-relative keys float with spot intraday, so marking an open
        position by leg-offset re-prices it to a different contract every bar
        — fatal for directional P&L. This follows the actual strike across
        whatever offsets it was recorded under. Contract identity uses
        (strike, type, expiry_kind, expiry_offset) because the backfilled
        `expiry` column is NULL; same-day lookups are unambiguous.
        Rows: (ts, close)."""
        return self._q(
            """SELECT ts, max(close) FROM option_bars
               WHERE underlying=? AND strike=? AND option_type=?
                 AND expiry_kind=? AND expiry_offset=? AND ts <= ?
               GROUP BY ts ORDER BY ts""",
            [underlying, strike, option_type, expiry_kind, expiry_offset, end])

    def coverage(self) -> tuple[list, dict]:
        """(underlying_bars summary rows, option_bars counts) for /data/coverage."""
        rows = self._q(
            """SELECT underlying, min(ts), max(ts), count(*) FROM underlying_bars
               GROUP BY underlying ORDER BY underlying""")
        opt = dict(self._q("SELECT underlying, count(*) FROM option_bars GROUP BY underlying"))
        return rows, opt

    # -- live recording (edge-research dataset) ------------------------------
    def upsert_chain_rows(self, rows: list[tuple]) -> int:
        """Full-fidelity chain snapshot rows (see chain_snapshots schema)."""
        if not rows:
            return 0
        with self._lock:
            self.con.executemany(
                "INSERT OR REPLACE INTO chain_snapshots VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def upsert_live_bar(self, underlying: str, bar) -> None:
        """Persist one completed live 5-min candle so the store stays current
        without re-backfilling (backtests can include today)."""
        with self._lock:
            self.con.execute(
                "INSERT OR REPLACE INTO underlying_bars VALUES (?,?,?,?,?,?,?,?)",
                [underlying, bar.ts, bar.open, bar.high, bar.low, bar.close,
                 getattr(bar, "volume", 0) or 0, getattr(bar, "oi", 0) or 0])

    def day_footprint(self, underlying: str, day: str) -> dict:
        """One session's chain-footprint aggregates for post-market analysis:
        per-strike OI build/unwind, IV open->close, PCR timeline, spot path,
        and the largest 5-min spot moves. Built from chain_snapshots (live
        recording) + underlying_bars."""
        strikes = self._q(
            """SELECT strike, option_type,
                      first(oi ORDER BY ts), last(oi ORDER BY ts),
                      first(iv ORDER BY ts), last(iv ORDER BY ts),
                      last(ltp ORDER BY ts), count(*)
               FROM chain_snapshots
               WHERE underlying=? AND CAST(ts AS DATE)=CAST(? AS DATE)
                 AND expiry_kind='WEEKLY' AND expiry_offset=0
               GROUP BY strike, option_type ORDER BY strike""",
            [underlying, day])
        pcr = self._q(
            """SELECT ts, sum(CASE WHEN option_type='PUT' THEN oi END)
                        / nullif(sum(CASE WHEN option_type='CALL' THEN oi END), 0)
               FROM chain_snapshots
               WHERE underlying=? AND CAST(ts AS DATE)=CAST(? AS DATE)
                 AND expiry_kind='WEEKLY' AND expiry_offset=0
               GROUP BY ts ORDER BY ts""",
            [underlying, day])
        spot = self._q(
            """SELECT ts, open, high, low, close FROM underlying_bars
               WHERE underlying=? AND CAST(ts AS DATE)=CAST(? AS DATE)
               ORDER BY ts""", [underlying, day])
        moves = sorted(
            ({"ts": str(r[0]), "move": round(r[4] - r[1], 1)} for r in spot),
            key=lambda m: abs(m["move"]), reverse=True)[:8]
        return {
            "underlying": underlying, "date": day,
            "strikes": [
                {"strike": s, "type": t,
                 "oi_open": o0, "oi_close": o1,
                 "oi_change": (o1 - o0) if (o0 is not None and o1 is not None) else None,
                 "iv_open": v0, "iv_close": v1, "ltp_close": ltp, "snapshots": n}
                for s, t, o0, o1, v0, v1, ltp, n in strikes],
            "pcr_timeline": [{"ts": str(a), "pcr": round(b, 3) if b else None}
                             for a, b in pcr],
            "spot": {"open": spot[0][1] if spot else None,
                     "close": spot[-1][4] if spot else None,
                     "high": max((r[2] for r in spot), default=None),
                     "low": min((r[3] for r in spot), default=None),
                     "bars": len(spot)},
            "largest_moves": moves,
        }

    def learning_stats(self) -> dict:
        """How much the analysis machinery has 'seen': recorded sessions and
        volume per underlying from the live chain recording, plus overall
        distinct learning days. Backs the Data tab maturity panel."""
        per = self._q(
            """SELECT underlying, min(CAST(ts AS DATE)), max(CAST(ts AS DATE)),
                      count(DISTINCT CAST(ts AS DATE)), count(*),
                      count(DISTINCT strike)
               FROM chain_snapshots GROUP BY underlying ORDER BY underlying""")
        total_days = self._q1(
            "SELECT count(DISTINCT CAST(ts AS DATE)) FROM chain_snapshots")
        total_rows = self._q1("SELECT count(*) FROM chain_snapshots")
        return {
            "underlyings": [
                {"underlying": u, "first_day": str(a), "last_day": str(b),
                 "sessions": d, "chain_rows": n, "strikes_seen": s}
                for u, a, b, d, n, s in per],
            "learning_days": (total_days or [0])[0],
            "chain_rows_total": (total_rows or [0])[0],
        }

    def purge_offhours(self, mcx_names: tuple = ("CRUDEOIL", "GOLD")) -> dict:
        """Delete recorded rows stamped outside exchange sessions (weekends /
        off-hours) — cleanup for the pre-gate recorder that re-stamped Friday's
        frozen chain as fresh data all weekend. NSE window 09:00-15:40,
        MCX 08:55-23:40, Mon-Fri."""
        out = {}
        mcx = list(mcx_names)
        with self._lock:
            # spot bars: junk ticks produced pre-open (09:00 auction) and
            # future-stamped bars (stale feed snapshots) — NSE session only
            before = self.con.execute("SELECT count(*) FROM underlying_bars").fetchone()[0]
            self.con.execute(f"""
                DELETE FROM underlying_bars WHERE
                  underlying NOT IN ({','.join('?' * len(mcx))})
                  AND (dayofweek(ts) IN (0, 6)
                       OR CAST(ts AS TIME) < TIME '09:15'
                       OR CAST(ts AS TIME) > TIME '15:30')
            """, mcx)
            after = self.con.execute("SELECT count(*) FROM underlying_bars").fetchone()[0]
            out["underlying_bars"] = {"deleted": before - after, "remaining": after}
            for table in ("option_bars", "chain_snapshots"):
                before = self.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                self.con.execute(f"""
                    DELETE FROM {table} WHERE
                      dayofweek(ts) IN (0, 6)              -- Sun / Sat
                      OR (underlying NOT IN ({','.join('?' * len(mcx))})
                          AND (CAST(ts AS TIME) < TIME '09:00'
                               OR CAST(ts AS TIME) > TIME '15:40'))
                      OR (underlying IN ({','.join('?' * len(mcx))})
                          AND (CAST(ts AS TIME) < TIME '08:55'
                               OR CAST(ts AS TIME) > TIME '23:40'))
                """, mcx + mcx)
                after = self.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                out[table] = {"deleted": before - after, "remaining": after}
        return out

    def checkpoint(self) -> None:
        """Force a DuckDB CHECKPOINT: flush the WAL into the main file and
        reclaim pages freed by deletes (purge_offhours). DuckDB does not
        shrink the file on DELETE alone, so on a small VPS space creeps up
        until a checkpoint runs. Cheap; safe to call from maintenance."""
        with self._lock:
            self.con.execute("CHECKPOINT")

    # -- FNO universe (scanner F1) -------------------------------------------
    def upsert_fno_universe(self, as_of, universe: dict) -> int:
        """Persist a dated snapshot of the resolved FNO stock universe.
        `universe` is {symbol: {...}} as returned by
        dhan_client.parse_fno_universe. Idempotent per (as_of, symbol)."""
        rows = [
            (str(as_of), sym, u.get("spot_security_id"),
             u.get("future_security_id"), u.get("fno_segment"),
             u.get("lot_size"), u.get("near_expiry"),
             ",".join(u.get("expiries") or []))
            for sym, u in universe.items()]
        if not rows:
            return 0
        with self._lock:
            self.con.executemany(
                "INSERT OR REPLACE INTO fno_universe VALUES (?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def fno_universe(self, as_of=None) -> list[dict]:
        """The FNO universe as of `as_of` (default: the latest snapshot).
        Returns [] if nothing has been resolved yet."""
        if as_of is None:
            row = self._q1("SELECT max(as_of) FROM fno_universe")
            as_of = row[0] if row and row[0] is not None else None
        if as_of is None:
            return []
        rows = self._q(
            """SELECT symbol, spot_security_id, future_security_id, fno_segment,
                      lot_size, near_expiry, expiries
               FROM fno_universe WHERE as_of=? ORDER BY symbol""", [str(as_of)])
        return [
            {"symbol": r[0], "spot_security_id": r[1], "future_security_id": r[2],
             "fno_segment": r[3], "lot_size": r[4],
             "near_expiry": str(r[5]) if r[5] is not None else None,
             "expiries": (r[6].split(",") if r[6] else [])}
            for r in rows]

    # -- Tier-1 scanner snapshots (F2) ---------------------------------------
    def upsert_stock_snapshots(self, rows: list[dict]) -> int:
        """Persist Tier-1 stock snapshots. `rows` are dicts as built by
        scanner.parse_stock_quote_rows. Idempotent per (symbol, ts)."""
        tup = [
            (r["symbol"], r["ts"], r.get("spot"), r.get("fut_ltp"),
             r.get("day_open"), r.get("day_high"), r.get("day_low"),
             r.get("prev_close"), r.get("volume"), r.get("oi"))
            for r in rows]
        if not tup:
            return 0
        with self._lock:
            self.con.executemany(
                "INSERT OR REPLACE INTO stock_snapshots VALUES (?,?,?,?,?,?,?,?,?,?)", tup)
        return len(tup)

    def stock_day_open_oi(self, symbol: str, day) -> tuple:
        """(first_oi, first_fut_ltp) of `day` for `symbol` — the start-of-day
        baseline OI/price the intraday buildup delta is measured against.
        (None, None) if the day has no rows yet."""
        row = self._q1(
            """SELECT first(oi ORDER BY ts), first(fut_ltp ORDER BY ts)
               FROM stock_snapshots
               WHERE symbol=? AND CAST(ts AS DATE)=CAST(? AS DATE)""",
            [symbol, str(day)])
        return (row[0], row[1]) if row else (None, None)

    def stock_volume_baseline(self, symbol: str, ref_tod_seconds: int,
                              before_day, days: int = 10):
        """Average cumulative day-volume at ~`ref_tod_seconds` past midnight,
        over the most recent `days` sessions strictly before `before_day`.
        The surge ratio is today's volume divided by this. None if no history."""
        row = self._q1(
            """WITH per_day AS (
                   SELECT CAST(ts AS DATE) AS d, volume,
                          row_number() OVER (
                              PARTITION BY CAST(ts AS DATE)
                              ORDER BY abs(hour(ts)*3600 + minute(ts)*60
                                           + second(ts) - ?)) AS rn
                   FROM stock_snapshots
                   WHERE symbol=? AND CAST(ts AS DATE) < CAST(? AS DATE))
               SELECT avg(volume) FROM (
                   SELECT volume FROM per_day WHERE rn=1 ORDER BY d DESC LIMIT ?)""",
            [ref_tod_seconds, symbol, str(before_day), days])
        return row[0] if row and row[0] is not None else None

    def latest_stock_snapshots(self, day=None) -> list[dict]:
        """Most recent snapshot per symbol for `day` (default: latest day with
        data). Feeds the scanner ranker."""
        if day is None:
            r = self._q1("SELECT max(CAST(ts AS DATE)) FROM stock_snapshots")
            day = r[0] if r and r[0] is not None else None
        if day is None:
            return []
        rows = self._q(
            """SELECT symbol, last(ts ORDER BY ts), last(spot ORDER BY ts),
                      last(fut_ltp ORDER BY ts), last(day_open ORDER BY ts),
                      last(day_high ORDER BY ts), last(day_low ORDER BY ts),
                      last(prev_close ORDER BY ts), last(volume ORDER BY ts),
                      last(oi ORDER BY ts)
               FROM stock_snapshots WHERE CAST(ts AS DATE)=CAST(? AS DATE)
               GROUP BY symbol ORDER BY symbol""", [str(day)])
        cols = ("symbol", "ts", "spot", "fut_ltp", "day_open", "day_high",
                "day_low", "prev_close", "volume", "oi")
        return [dict(zip(cols, r)) for r in rows]

    # -- Index bias (F5) -----------------------------------------------------
    def upsert_index_bias(self, ts, index_name: str, bias: dict) -> None:
        """Record one bias reading. `bias` is scanner.index_bias() output."""
        with self._lock:
            self.con.execute(
                "INSERT OR REPLACE INTO index_bias_history VALUES (?,?,?,?,?,?,?,?,?,?)",
                [ts, index_name, bias.get("score"), bias.get("buildup_breadth"),
                 bias.get("price_breadth"), bias.get("bull_weight"),
                 bias.get("bear_weight"), bias.get("coverage"), bias.get("n"),
                 bias.get("spot")])

    def recent_index_bias(self, index_name: str, limit: int = 60) -> list[dict]:
        """Most recent bias readings for one index, oldest-first (for a spark
        line). Returns dicts of ts/score/breadth/spot."""
        rows = self._q(
            """SELECT ts, score, buildup_breadth, price_breadth, coverage, n, spot
               FROM index_bias_history WHERE index_name=?
               ORDER BY ts DESC LIMIT ?""", [index_name, limit])
        cols = ("ts", "score", "buildup_breadth", "price_breadth", "coverage", "n", "spot")
        return [dict(zip(cols, r)) for r in reversed(rows)]

    def index_bias_on(self, day, index_name: str) -> list[tuple]:
        """(ts, score, spot) rows recorded on `day` for accuracy scoring."""
        return self._q(
            """SELECT ts, score, spot FROM index_bias_history
               WHERE index_name=? AND CAST(ts AS DATE)=CAST(? AS DATE)
               ORDER BY ts""", [index_name, str(day)])

    def upsert_index_bias_accuracy(self, day, index_name: str, horizon_min: int,
                                   n: int, hits: int, avg_move: float) -> None:
        hit_rate = (hits / n) if n else None
        with self._lock:
            self.con.execute(
                "INSERT OR REPLACE INTO index_bias_accuracy VALUES (?,?,?,?,?,?,?)",
                [str(day), index_name, horizon_min, n, hits, hit_rate, avg_move])

    def index_bias_accuracy(self, index_name: str, limit: int = 30) -> list[dict]:
        rows = self._q(
            """SELECT day, horizon_min, n, hits, hit_rate, avg_forward_move
               FROM index_bias_accuracy WHERE index_name=?
               ORDER BY day DESC LIMIT ?""", [index_name, limit])
        cols = ("day", "horizon_min", "n", "hits", "hit_rate", "avg_forward_move")
        return [dict(zip(cols, [str(r[0]) if i == 0 else r[i]
                                for i, _ in enumerate(cols)])) for r in rows]

    def latest_spot(self, underlying: str, day=None):
        """Last recorded spot close for `underlying` (default: latest day).
        Used to stamp the index price alongside a bias reading."""
        if day is None:
            row = self._q1(
                """SELECT last(close ORDER BY ts) FROM underlying_bars
                   WHERE underlying=?""", [underlying])
        else:
            row = self._q1(
                """SELECT last(close ORDER BY ts) FROM underlying_bars
                   WHERE underlying=? AND CAST(ts AS DATE)=CAST(? AS DATE)""",
                [underlying, str(day)])
        return row[0] if row and row[0] is not None else None

    def spot_bars_on(self, underlying: str, day) -> list[tuple]:
        """(ts, close) spot bars for `underlying` on `day`, ascending — for
        computing the realized forward move behind bias accuracy."""
        return self._q(
            """SELECT ts, close FROM underlying_bars
               WHERE underlying=? AND CAST(ts AS DATE)=CAST(? AS DATE)
               ORDER BY ts""", [underlying, str(day)])

    # -- Setup flags / validation (F6) ---------------------------------------
    def record_setup_flag(self, ts, symbol: str, bias: str, score: float,
                          spot, atm_premium) -> None:
        with self._lock:
            self.con.execute(
                "INSERT OR REPLACE INTO setup_flags VALUES (?,?,?,?,?,?)",
                [ts, symbol, bias, score, spot, atm_premium])

    def setup_flags_since(self, since_day) -> list[dict]:
        rows = self._q(
            """SELECT ts, symbol, bias, score, spot, atm_premium
               FROM setup_flags WHERE CAST(ts AS DATE) >= CAST(? AS DATE)
               ORDER BY ts""", [str(since_day)])
        cols = ("ts", "symbol", "bias", "score", "spot", "atm_premium")
        return [dict(zip(cols, r)) for r in rows]

    def atm_premium_at(self, symbol: str, ts, side: str, window_min: int = 20):
        """LTP of the ~ATM option (`side` = CALL/PUT) for `symbol` at the
        chain snapshot nearest `ts` within `window_min`. Backs forward-return
        scoring of a flagged setup. None if nothing recorded near then."""
        row = self._q1(
            """SELECT ltp FROM chain_snapshots
               WHERE underlying=? AND option_type=?
                 AND ts BETWEEN ? - INTERVAL (? || ' minutes')
                             AND ? + INTERVAL (? || ' minutes')
               ORDER BY abs(strike_offset),
                        abs(date_diff('second', ts, ?)) LIMIT 1""",
            [symbol, side, ts, str(window_min), ts, str(window_min), ts])
        return row[0] if row and row[0] is not None else None

    def recording_status(self) -> list[dict]:
        """Per-underlying live-recording counters for the Data tab: what's been
        captured today (IST) in chain_snapshots + live spot bars."""
        chain = self._q(
            """SELECT underlying, count(*), max(ts),
                      count(DISTINCT strike), count(DISTINCT expiry)
               FROM chain_snapshots WHERE CAST(ts AS DATE) = CURRENT_DATE
               GROUP BY underlying""")
        spot = dict((r[0], (r[1], r[2])) for r in self._q(
            """SELECT underlying, count(*), max(ts) FROM underlying_bars
               WHERE CAST(ts AS DATE) = CURRENT_DATE GROUP BY underlying"""))
        out = {}
        for u, n, last, strikes, expiries in chain:
            out[u] = {"underlying": u, "chain_rows_today": n,
                      "last_snapshot": str(last), "strikes": strikes,
                      "expiries": expiries, "spot_bars_today": 0,
                      "last_spot_bar": None}
        for u, (n, last) in spot.items():
            rec = out.setdefault(u, {"underlying": u, "chain_rows_today": 0,
                                     "last_snapshot": None, "strikes": 0,
                                     "expiries": 0})
            rec["spot_bars_today"] = n
            rec["last_spot_bar"] = str(last)
        return sorted(out.values(), key=lambda r: r["underlying"])


class SyntheticStore:
    """Deterministic fake market so the platform runs with zero setup.
    Spot follows a random walk; option prices come from a crude
    Black-Scholes-ish decay so straddle strategies behave sensibly."""

    STRIKE_STEP = {"NIFTY": 50.0, "BANKNIFTY": 100.0}

    def __init__(self, seed: int = 42, base: float = 22000.0):
        self.seed, self.base = seed, base

    def _step(self, u): return self.STRIKE_STEP.get(u, 50.0)

    def underlying_bars(self, underlying: str, start: datetime, end: datetime,
                        interval_min: int) -> list[Bar]:
        rng = random.Random(f"{self.seed}-{underlying}-{start.date()}")
        bars, px, ts = [], self.base, start
        while ts <= end:
            if time(9, 15) <= ts.time() <= time(15, 30) and ts.weekday() < 5:
                drift = rng.gauss(0, px * 0.0006)
                o = px
                c = max(1.0, px + drift)
                h = max(o, c) * (1 + abs(rng.gauss(0, 3e-4)))
                low = min(o, c) * (1 - abs(rng.gauss(0, 3e-4)))
                bars.append(Bar(ts, o, h, low, c, rng.randint(1_000, 9_000)))
                px = c
            ts += timedelta(minutes=interval_min)
        return bars

    def option_close(self, underlying: str, ts: datetime, leg: LegSpec):
        # Recreate the spot path deterministically, then price crudely.
        day_start = ts.replace(hour=9, minute=15, second=0, microsecond=0)
        bars = self.underlying_bars(underlying, day_start, ts, 5)
        spot = bars[-1].close if bars else self.base
        step = self._step(underlying)
        atm = round(spot / step) * step
        strike = atm + leg.strike_offset * step
        days_ahead = (3 - ts.weekday()) % 7  # next Thursday
        expiry = (ts + timedelta(days=days_ahead + 7 * leg.expiry_offset)).date()
        t_years = max(1e-4, ((datetime.combine(expiry, time(15, 30)) - ts)
                             .total_seconds() / (365 * 24 * 3600)))
        iv = 0.14
        intrinsic = max(0.0, (spot - strike) if leg.option_type == OptionType.CALL
                        else (strike - spot))
        time_val = 0.4 * spot * iv * math.sqrt(t_years) * \
            math.exp(-abs(strike - spot) / (spot * iv * math.sqrt(t_years) + 1e-9))
        ltp = round(max(0.05, intrinsic + time_val), 2)
        return OptionQuote(ts, underlying, expiry, strike, leg.option_type,
                           ltp=ltp, bid=round(ltp * 0.997, 2), ask=round(ltp * 1.003, 2),
                           iv=iv * 100)


def _duck_has(self, underlying: str, start, end) -> bool:
    row = self._q1(
        "SELECT count(*) FROM underlying_bars WHERE underlying=? AND ts BETWEEN ? AND ?",
        [underlying, start, end])
    return bool(row and row[0] > 0)   # locked query; row is never None but be safe


DataStore.has_data = _duck_has
SyntheticStore.has_data = lambda self, u, s, e: True
# recording is a no-op on the synthetic store (nothing real to persist)
SyntheticStore.recording_status = lambda self: []
SyntheticStore.upsert_chain_rows = lambda self, rows: 0
SyntheticStore.upsert_live_bar = lambda self, u, b: None
SyntheticStore.learning_stats = lambda self: {
    "underlyings": [], "learning_days": 0, "chain_rows_total": 0}
SyntheticStore.upsert_fno_universe = lambda self, as_of, universe: 0
SyntheticStore.fno_universe = lambda self, as_of=None: []
SyntheticStore.upsert_stock_snapshots = lambda self, rows: 0
SyntheticStore.stock_day_open_oi = lambda self, symbol, day: (None, None)
SyntheticStore.stock_volume_baseline = lambda self, s, r, b, days=10: None
SyntheticStore.latest_stock_snapshots = lambda self, day=None: []
SyntheticStore.upsert_index_bias = lambda self, ts, name, bias: None
SyntheticStore.recent_index_bias = lambda self, name, limit=60: []
SyntheticStore.index_bias_accuracy = lambda self, name, limit=30: []
SyntheticStore.latest_spot = lambda self, u, day=None: None
SyntheticStore.record_setup_flag = lambda self, ts, s, b, sc, sp, ap: None
SyntheticStore.setup_flags_since = lambda self, since_day: []
SyntheticStore.atm_premium_at = lambda self, s, ts, side, window_min=20: None


def get_store(prefer_real: bool = True):
    if prefer_real:
        try:
            import duckdb  # noqa: F401
            store = DataStore()
            n = store.con.execute("SELECT count(*) FROM underlying_bars").fetchone()[0]
            if n > 0:
                return store
        except Exception:
            pass
    return SyntheticStore()
