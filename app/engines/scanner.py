"""
FNO Stock Scanner — Tier 1 (F2)
===============================
Broad-and-cheap sweep of the whole FNO stock universe. One batched market-quote
call per minute (Dhan `quote_data`, all ~190 stock futures + their cash equities)
gives price, day OHLC, cumulative volume and futures OI for every name in a single
request — orders of magnitude cheaper than per-name polling and nowhere near the
option-chain rate limit (that limit binds Tier 2, not this).

Layering (invariant: never starve the paper-fill chain poller):
  * Tier 1 (here)  — quote sweep, whole universe, ~1/min. Cheap REST.
  * Tier 2 (F3)    — option chain, SHORTLIST only, through MarketHub's 3s gate.

This module is PURE where it can be: buildup classification, volume surge and
quote parsing are free functions exercised offline (tests/test_scanner.py).
`StockScanner` wraps them in the same asyncio+executor pattern MarketHub uses
(per-cycle client rebuild on error, market-hours gate, registry events).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

# NSE cash/derivatives session (same window main._session_open uses for NSE).
_SESSION_START_MIN = 9 * 60 + 15
_SESSION_END_MIN = 15 * 60 + 35

POLL_INTERVAL = 60.0      # one universe sweep per minute
QUOTE_BATCH = 900         # instruments per quote_data call (< Dhan's ~1000 cap)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def classify_buildup(price_change, oi_change, flat_eps: float = 0.0) -> str:
    """The four classic futures-OI regimes from intraday deltas:
        price up   + OI up   -> long_buildup     (fresh longs, bullish)
        price down + OI up   -> short_buildup    (fresh shorts, bearish)
        price up   + OI down -> short_covering   (shorts exiting, bullish-ish)
        price down + OI down -> long_unwinding   (longs exiting, bearish-ish)
    `flat_eps` treats tiny moves as flat -> 'neutral'. 'unknown' if a delta is
    missing (no baseline yet)."""
    if price_change is None or oi_change is None:
        return "unknown"
    up_p, up_o = price_change > flat_eps, oi_change > flat_eps
    dn_p, dn_o = price_change < -flat_eps, oi_change < -flat_eps
    if up_p and up_o:
        return "long_buildup"
    if dn_p and up_o:
        return "short_buildup"
    if up_p and dn_o:
        return "short_covering"
    if dn_p and dn_o:
        return "long_unwinding"
    return "neutral"


def volume_surge(current_vol, baseline_vol):
    """current cumulative volume / baseline (trailing-N-day avg at same
    time-of-day). >1 means busier than usual. None if no baseline."""
    if current_vol is None or not baseline_vol or baseline_vol <= 0:
        return None
    return current_vol / baseline_vol


def pct_change(now, ref):
    if now is None or ref in (None, 0):
        return None
    return (now - ref) / ref * 100.0


def parse_stock_quote_rows(universe: dict, quote_data: dict, ts) -> list[dict]:
    """Map a batched quote response to one snapshot dict per symbol.

    `universe` is {symbol: {...}} from dhan_client.parse_fno_universe (used to
    reverse security_id -> symbol). `quote_data` is {segment: {sid_str: node}}
    from dhan_client.fetch_quotes. Future node -> price/OHLC/volume/OI; cash
    node -> spot."""
    fut_by_sid, spot_by_sid = {}, {}
    for sym, u in universe.items():
        if u.get("future_security_id") is not None:
            fut_by_sid[str(u["future_security_id"])] = sym
        if u.get("spot_security_id") is not None:
            spot_by_sid[str(u["spot_security_id"])] = sym

    nodes = {}
    for seg, m in (quote_data or {}).items():
        if isinstance(m, dict):
            for sid, node in m.items():
                nodes[str(sid)] = node

    out: dict[str, dict] = {}
    for sid, sym in fut_by_sid.items():
        node = nodes.get(sid)
        if not node:
            continue
        ohlc = node.get("ohlc") or {}
        out[sym] = {
            "symbol": sym, "ts": ts,
            "fut_ltp": _f(node.get("last_price")),
            "day_open": _f(ohlc.get("open")),
            "day_high": _f(ohlc.get("high")),
            "day_low": _f(ohlc.get("low")),
            "prev_close": _f(ohlc.get("close")),
            "volume": _f(node.get("volume")),
            "oi": _f(node.get("oi")),
            "spot": None,
        }
    for sid, sym in spot_by_sid.items():
        node = nodes.get(sid)
        if not node:
            continue
        rec = out.setdefault(sym, {
            "symbol": sym, "ts": ts, "fut_ltp": None, "day_open": None,
            "day_high": None, "day_low": None, "prev_close": None,
            "volume": None, "oi": None, "spot": None})
        rec["spot"] = _f(node.get("last_price"))
    return list(out.values())


def compute_metrics(snap: dict, day_open_oi, day_open_price, vol_baseline) -> dict:
    """Per-symbol Tier-1 metrics from one snapshot + its day baselines.
    Pure — the poller (or a test) supplies baselines from the store."""
    ltp = snap.get("fut_ltp")
    prev_close = snap.get("prev_close")
    oi = snap.get("oi")
    price_chg = pct_change(ltp, prev_close)
    # OI delta uses the day's FIRST recorded OI as the intraday baseline.
    oi_chg = None
    if oi is not None and day_open_oi not in (None, 0):
        oi_chg = (oi - day_open_oi) / day_open_oi * 100.0
    surge = volume_surge(snap.get("volume"), vol_baseline)
    hi, lo = snap.get("day_high"), snap.get("day_low")
    # position in the day's range (1 = at high, 0 = at low)
    range_pos = None
    if ltp is not None and hi is not None and lo is not None and hi > lo:
        range_pos = (ltp - lo) / (hi - lo)
    return {
        "symbol": snap["symbol"],
        "ltp": ltp, "spot": snap.get("spot"),
        "price_change_pct": price_chg,
        "oi_change_pct": oi_chg,
        "buildup": classify_buildup(price_chg, oi_chg),
        "volume_surge": surge,
        "range_pos": range_pos,
    }


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------

def _session_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return _SESSION_START_MIN <= mins <= _SESSION_END_MIN


def _batches(ids: list, n: int):
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


class StockScanner:
    """Tier-1 universe sweep. Persists snapshots to `store.stock_snapshots`
    and keeps the latest per-symbol metrics in memory for the ranker (F3)."""

    def __init__(self, store):
        self.store = store
        self._universe: dict = {}
        self._universe_day = None
        self.metrics: dict[str, dict] = {}     # symbol -> latest metrics
        self._last_sweep_ts = None

    def _securities(self) -> dict:
        """{segment: [security_id...]} for the whole universe — futures under
        their fno_segment, cash equities under NSE_EQ."""
        fno, eq = [], []
        for u in self._universe.values():
            if u.get("future_security_id") is not None:
                fno.append(int(u["future_security_id"]))
            if u.get("spot_security_id") is not None:
                eq.append(int(u["spot_security_id"]))
        out = {}
        if fno:
            out["NSE_FNO"] = fno
        if eq:
            out["NSE_EQ"] = eq
        return out

    async def _ensure_universe(self, loop) -> None:
        today = datetime.now(IST).date()
        if self._universe and self._universe_day == today:
            return
        from app.data import dhan_client
        uni = await loop.run_in_executor(
            None, lambda: dhan_client.resolve_fno_universe(store=self.store))
        if uni:
            self._universe = uni
            self._universe_day = today

    async def sweep_once(self, client, loop) -> int:
        """One full-universe quote sweep -> persist + refresh metrics.
        Returns the number of symbols updated. Batches the quote call to stay
        under the per-request instrument cap."""
        securities = self._securities()
        if not securities:
            return 0
        from app.data import dhan_client
        # Fetch in <=QUOTE_BATCH chunks per segment, merge into one payload.
        merged: dict[str, dict] = {}
        seg_ids = {seg: list(ids) for seg, ids in securities.items()}
        while any(seg_ids.values()):
            req = {}
            for seg, ids in seg_ids.items():
                if ids:
                    req[seg] = ids[:QUOTE_BATCH]
                    seg_ids[seg] = ids[QUOTE_BATCH:]
            data = await loop.run_in_executor(
                None, dhan_client.fetch_quotes, client, req)
            for seg, m in (data or {}).items():
                merged.setdefault(seg, {}).update(m or {})
            await asyncio.sleep(1.0)   # ~1 req/s courtesy spacing

        ts = datetime.now(IST).replace(tzinfo=None)
        snaps = parse_stock_quote_rows(self._universe, merged, ts)
        if not snaps:
            return 0
        self.store.upsert_stock_snapshots(snaps)
        day = ts.date()
        ref_tod = ts.hour * 3600 + ts.minute * 60 + ts.second
        for snap in snaps:
            sym = snap["symbol"]
            day_open_oi, day_open_price = self.store.stock_day_open_oi(sym, day)
            baseline = self.store.stock_volume_baseline(sym, ref_tod, day)
            self.metrics[sym] = compute_metrics(
                snap, day_open_oi, day_open_price, baseline)
        self._last_sweep_ts = ts
        return len(snaps)

    async def run(self) -> None:
        """Long-lived poll loop. Market-hours gated; rebuilds the Dhan client
        on any error (the 24h token rotates mid-session)."""
        from app.core import registry
        from app.data import dhan_client
        loop = asyncio.get_running_loop()
        client = None
        await asyncio.sleep(20)   # let the app settle before the first sweep
        while True:
            try:
                if registry.setting("scanner", "off") != "on":
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                if not _session_open():
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                if not hasattr(self.store, "con"):
                    await asyncio.sleep(POLL_INTERVAL)
                    continue           # synthetic store — nothing to persist
                await self._ensure_universe(loop)
                if client is None:
                    client = await loop.run_in_executor(None, dhan_client.get_client)
                n = await self.sweep_once(client, loop)
                if n:
                    registry.record_event("info", "scanner",
                                           f"tier-1 sweep: {n} stocks")
            except Exception as e:
                registry.record_event("warn", "scanner", f"sweep error: {e!r}")
                client = None
            await asyncio.sleep(POLL_INTERVAL)
