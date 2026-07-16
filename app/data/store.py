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
