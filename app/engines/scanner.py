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

SHORTLIST_SIZE = 15       # Tier-2 deep-dive only this many movers
TIER2_INTERVAL = 300.0    # re-rank + re-poll the shortlist every 5 min
# Indian stock options are MONTHLY (no weeklies) — poll the nearest month.
STOCK_CHAIN_TARGETS = (("MONTHLY", 0),)

# Directional read of each buildup regime for an option-BUYING bias.
_BIAS = {"long_buildup": "CE", "short_covering": "CE",
         "short_buildup": "PE", "long_unwinding": "PE"}


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
# Tier-2 ranking + chain analytics (pure)
# ---------------------------------------------------------------------------

def rank_shortlist(metrics: dict, top_n: int = SHORTLIST_SIZE,
                   min_abs_move: float = 0.3) -> list[dict]:
    """Rank Tier-1 metrics into the Tier-2 shortlist. Score rewards a bigger
    move, a volume surge, and OI-buildup that CONFIRMS the move (fresh
    positioning beats a move on shrinking OI). Names moving less than
    `min_abs_move`% are ignored (chop). Returns the top N with a buy bias."""
    scored = []
    for sym, m in metrics.items():
        pc = m.get("price_change_pct")
        if pc is None or abs(pc) < min_abs_move:
            continue
        surge = m.get("volume_surge") or 1.0
        buildup = m.get("buildup")
        # fresh buildup (long/short) confirms hardest; covering/unwinding is
        # weaker fuel; neutral/unknown barely scores.
        align = (1.0 if buildup in ("long_buildup", "short_buildup")
                 else 0.6 if buildup in ("short_covering", "long_unwinding")
                 else 0.2)
        score = abs(pc) * align * min(surge, 5.0)
        scored.append((score, sym, m, buildup))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [
        {"symbol": sym, "score": round(sc, 3), "buildup": b,
         "bias": _BIAS.get(b),
         "price_change_pct": m.get("price_change_pct"),
         "volume_surge": m.get("volume_surge"),
         "range_pos": m.get("range_pos")}
        for sc, sym, m, b in scored[:top_n]]


def chain_metrics(cache: dict, atm_window: int = 3) -> dict:
    """PCR / ATM-IV / IV-skew from a hub chain cache
    ({(kind, off, strike_offset, otype): OptionQuote}). Skew = avg OTM-put IV
    minus avg OTM-call IV within `atm_window` strikes — positive = downside
    fear (puts bid up), the classic pre-fall tell."""
    calls = [q for k, q in cache.items() if k[3] == "CALL"]
    puts = [q for k, q in cache.items() if k[3] == "PUT"]

    def _sum(qs, attr):
        return sum(getattr(q, attr) or 0 for q in qs)

    call_oi, put_oi = _sum(calls, "oi"), _sum(puts, "oi")
    call_vol, put_vol = _sum(calls, "volume"), _sum(puts, "volume")
    atm_ivs = [q.iv for k, q in cache.items() if k[2] == 0 and q.iv]
    otm_put_iv = [q.iv for k, q in cache.items()
                  if k[3] == "PUT" and -atm_window <= k[2] < 0 and q.iv]
    otm_call_iv = [q.iv for k, q in cache.items()
                   if k[3] == "CALL" and 0 < k[2] <= atm_window and q.iv]

    def _avg(xs):
        return sum(xs) / len(xs) if xs else None

    skew = None
    if otm_put_iv and otm_call_iv:
        skew = _avg(otm_put_iv) - _avg(otm_call_iv)
    return {
        "pcr_oi": (put_oi / call_oi) if call_oi else None,
        "pcr_volume": (put_vol / call_vol) if call_vol else None,
        "atm_iv": _avg(atm_ivs),
        "iv_skew": skew,
        "call_oi": call_oi, "put_oi": put_oi,
    }


def liquidity_screen(cache: dict, atm_window: int = 2,
                     max_spread_pct: float = 2.0, min_oi: float = 0.0) -> dict:
    """A stock-option BUY is only real if you can get out. Check near-ATM
    strikes: bid-ask spread as % of mid, and an OI floor. `ok` is False if any
    checked strike is too wide or too thin — the single most important gate
    for stock options, which get illiquid fast outside the top names."""
    checked = bad = 0
    worst_spread = 0.0
    for k, q in cache.items():
        if abs(k[2]) > atm_window:
            continue
        if q.bid is None or q.ask is None:
            continue
        mid = (q.bid + q.ask) / 2
        if mid <= 0:
            continue
        checked += 1
        spread_pct = (q.ask - q.bid) / mid * 100
        worst_spread = max(worst_spread, spread_pct)
        if spread_pct > max_spread_pct or (q.oi or 0) < min_oi:
            bad += 1
    if checked == 0:
        return {"ok": False, "checked": 0, "bad": 0,
                "worst_spread_pct": None, "reason": "no two-sided quotes"}
    return {"ok": bad == 0, "checked": checked, "bad": bad,
            "worst_spread_pct": round(worst_spread, 2),
            "reason": "" if bad == 0 else f"{bad}/{checked} strikes illiquid"}


def oi_shift(prev_cache: dict, cur_cache: dict, top: int = 6) -> list[dict]:
    """Per-strike OI change between two chain snapshots of the same name —
    where positioning is building or unwinding (support/resistance walls
    forming or breaking). Largest absolute moves first."""
    out = []
    for k, q in cur_cache.items():
        p = prev_cache.get(k)
        if p is None or p.oi is None or q.oi is None:
            continue
        out.append({"strike_offset": k[2], "option_type": k[3],
                    "oi_change": q.oi - p.oi})
    out.sort(key=lambda d: abs(d["oi_change"]), reverse=True)
    return out[:top]


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
        self.metrics: dict[str, dict] = {}     # symbol -> latest Tier-1 metrics
        self._last_sweep_ts = None
        self.shortlist: list[dict] = []        # latest Tier-2 shortlist (ranked)
        self.tier2: dict[str, dict] = {}       # symbol -> Tier-2 chain analytics
        self._prev_chain: dict[str, dict] = {} # symbol -> last chain cache (OI shift)

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

    # -- Tier 2: shortlist chain deep-dive -----------------------------------
    def _register_chain_cfgs(self, symbols) -> list[str]:
        """Inject shortlisted stocks into dhan_client.UNDERLYINGS so the shared
        chain poller can fetch them (option_chain keys off the cash-equity id
        + NSE_EQ, the way resolve_mcx_ids injects MCX names). Returns the names
        that actually have a spot id to poll."""
        from app.data.dhan_client import UNDERLYINGS
        ready = []
        for sym in symbols:
            u = self._universe.get(sym)
            if not u or u.get("spot_security_id") is None:
                continue
            UNDERLYINGS[sym] = {
                "security_id": int(u["spot_security_id"]),
                "segment": "NSE_EQ", "fno_segment": u.get("fno_segment", "NSE_FNO"),
                "instrument": "OPTSTK"}
            ready.append(sym)
        return ready

    async def tier2_once(self, hub, loop) -> int:
        """Re-rank Tier-1 metrics, then deep-dive the shortlist's option chains
        THROUGH THE HUB'S shared gate (so it's globally rate-limited with, and
        yields priority to, the deployed poller). Persists full chain rows and
        computes per-symbol PCR/IV/skew/OI-shift + liquidity. Returns how many
        names were analysed."""
        from app.core import registry
        from app.data import dhan_client
        self.shortlist = rank_shortlist(self.metrics)
        symbols = self._register_chain_cfgs(d["symbol"] for d in self.shortlist)
        if not symbols:
            return 0
        from app.data.dhan_client import UNDERLYINGS
        client = await loop.run_in_executor(None, dhan_client.get_client)
        done = 0
        for sym in symbols:
            cfg = UNDERLYINGS.get(sym)
            try:
                await hub._poll_one_chain(sym, cfg, client, loop, STOCK_CHAIN_TARGETS)
            except Exception as e:
                registry.record_event("warn", "scanner", f"tier2 chain [{sym}]: {e!r}")
                continue
            cache = hub._chain_cache.get(sym) or {}
            if not cache:
                continue
            metrics = chain_metrics(cache)
            metrics["liquidity"] = liquidity_screen(cache)
            prev = self._prev_chain.get(sym)
            metrics["oi_shift"] = oi_shift(prev, cache) if prev else []
            self._prev_chain[sym] = dict(cache)
            self.tier2[sym] = metrics
            done += 1
        # persist the shortlist's full chains into the research dataset
        if hasattr(self.store, "upsert_chain_rows"):
            try:
                hub.persist_chain_full(self.store, underlyings=set(symbols))
            except Exception as e:
                registry.record_event("warn", "scanner", f"tier2 persist: {e!r}")
        if done:
            registry.record_event("info", "scanner", f"tier-2 deep-dive: {done} names")
        return done

    async def run_tier2(self, hub) -> None:
        """Long-lived Tier-2 loop: every TIER2_INTERVAL, re-rank and deep-dive
        the shortlist. Gated by the same `scanner` setting + session window."""
        from app.core import registry
        loop = asyncio.get_running_loop()
        await asyncio.sleep(90)   # let Tier-1 accumulate a first sweep or two
        while True:
            try:
                if (registry.setting("scanner", "off") == "on"
                        and _session_open() and self.metrics
                        and hasattr(self.store, "con")):
                    await hub.ensure_started()
                    await self.tier2_once(hub, loop)
            except Exception as e:
                registry.record_event("warn", "scanner", f"tier2 loop: {e!r}")
            await asyncio.sleep(TIER2_INTERVAL)

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
