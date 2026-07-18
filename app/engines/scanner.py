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

# Approximate constituent weights (%), as of 2026 — a bias indicator, not an
# index-replication basket, so approximate is fine. Refresh from NSE factsheets
# periodically; a symbol absent from the Tier-1 sweep is simply skipped (its
# weight drops out of the coverage denominator). BANKNIFTY is the bank subset.
INDEX_CONSTITUENTS = {
    "BANKNIFTY": {
        "HDFCBANK": 28.0, "ICICIBANK": 24.0, "SBIN": 9.5, "AXISBANK": 9.0,
        "KOTAKBANK": 8.0, "PNB": 3.0, "BANKBARODA": 2.7, "INDUSINDBK": 2.5,
        "AUBANK": 2.2, "FEDERALBNK": 2.0, "IDFCFIRSTB": 1.6, "CANBK": 1.5,
    },
    "NIFTY": {
        "HDFCBANK": 12.0, "ICICIBANK": 8.5, "RELIANCE": 8.0, "INFY": 5.0,
        "TCS": 4.0, "ITC": 3.8, "LT": 3.6, "AXISBANK": 3.2, "SBIN": 3.0,
        "BHARTIARTL": 3.0, "KOTAKBANK": 2.8, "HINDUNILVR": 2.5, "BAJFINANCE": 2.2,
        "M&M": 2.0, "MARUTI": 1.9, "SUNPHARMA": 1.8, "TATAMOTORS": 1.7,
        "NTPC": 1.6, "HCLTECH": 1.5, "TITAN": 1.3,
    },
}

# Which index each bank/sector maps to for sector-bucketed flow (extendable).
_bias_label = lambda s: ("bullish" if s is not None and s > 0.3
                         else "bearish" if s is not None and s < -0.3
                         else "neutral")


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


def setup_score(t1: dict, t2: dict | None = None) -> dict:
    """Composite option-BUYING setup score (0-100) with per-component reasons.
    Combines the Tier-1 read (move size, buildup confirmation, volume surge,
    range position) with Tier-2 chain quality (IV not already spiked, liquidity
    passable, OI shift confirming). Every component appends a human reason so
    the UI/alert explains WHY — a score with no reasons is not actionable.

    `t1` is a compute_metrics() dict; `t2` is a chain_metrics() dict (+
    'liquidity'/'oi_shift') or None if the name wasn't deep-dived yet."""
    reasons: list[str] = []
    pc = t1.get("price_change_pct") or 0.0
    buildup = t1.get("buildup")
    bias = _BIAS.get(buildup)
    score = 0.0

    # 1) move magnitude (up to 25) — needs a real move to buy momentum
    move_pts = min(abs(pc) / 3.0, 1.0) * 25
    score += move_pts
    if abs(pc) >= 0.3:
        reasons.append(f"moved {pc:+.1f}%")

    # 2) buildup confirmation (up to 30) — fresh positioning is the thesis
    if buildup in ("long_buildup", "short_buildup"):
        score += 30
        reasons.append(f"{buildup.replace('_', ' ')} (fresh OI)")
    elif buildup in ("short_covering", "long_unwinding"):
        score += 15
        reasons.append(buildup.replace("_", " "))

    # 3) volume surge (up to 20)
    surge = t1.get("volume_surge")
    if surge:
        score += min(surge / 3.0, 1.0) * 20
        if surge >= 1.5:
            reasons.append(f"{surge:.1f}x volume")

    # 4) range position aligned with bias (up to 10)
    rp = t1.get("range_pos")
    if rp is not None and bias:
        aligned = rp if bias == "CE" else (1 - rp)
        score += aligned * 10
        if aligned >= 0.7:
            reasons.append("pressing the day's " + ("high" if bias == "CE" else "low"))

    # 5) Tier-2 chain quality (up to 15, and a hard liquidity veto)
    if t2:
        liq = t2.get("liquidity") or {}
        if liq.get("ok"):
            score += 8
            reasons.append("liquid chain")
        elif liq.get("checked"):
            # illiquid stock options you can't exit — cap the score hard
            score = min(score, 35)
            reasons.append("ILLIQUID: " + (liq.get("reason") or "wide spreads"))
        skew = t2.get("iv_skew")
        if skew is not None and bias:
            # downside fear (skew>0) helps PE buys, hurts CE buys
            if (bias == "PE" and skew > 0) or (bias == "CE" and skew < 0):
                score += 4
                reasons.append("IV skew confirms")
        oi_sh = t2.get("oi_shift") or []
        if oi_sh:
            top = oi_sh[0]
            reasons.append(
                f"OI {'+' if top['oi_change'] > 0 else ''}{int(top['oi_change'])}"
                f" @ {top['option_type']}{top['strike_offset']:+d}")
            score += 3

    return {"symbol": t1.get("symbol"), "score": round(min(score, 100.0), 1),
            "bias": bias, "buildup": buildup, "reasons": reasons,
            "price_change_pct": pc, "volume_surge": surge,
            "deep_dived": bool(t2)}


def hitrate_stats(rows: list[dict]) -> dict:
    """Forward-return hit-rate of flagged setups (F6 validation). Each row:
    {score, entry, exit} — entry/exit are the bias-side ATM premiums at flag
    time and at the horizon. A 'hit' is exit > entry (buying the option paid).
    Bucketed by score band so we can see whether higher scores actually predict
    better outcomes. Pure — the store/job assembles rows; a test feeds literals."""
    buckets = {"70-100": [], "55-70": [], "40-55": [], "<40": []}

    def _band(s):
        return ("70-100" if s >= 70 else "55-70" if s >= 55
                else "40-55" if s >= 40 else "<40")

    usable = []
    for r in rows:
        e, x = r.get("entry"), r.get("exit")
        if e in (None, 0) or x is None:
            continue
        ret = (x - e) / e * 100.0
        usable.append(ret)
        buckets[_band(r.get("score") or 0)].append(ret)

    def _summ(rets):
        n = len(rets)
        hits = sum(1 for r in rets if r > 0)
        return {"n": n, "hits": hits,
                "hit_rate": round(hits / n, 3) if n else None,
                "avg_return_pct": round(sum(rets) / n, 2) if n else None}

    return {"overall": _summ(usable),
            "by_score": {k: _summ(v) for k, v in buckets.items()}}


def alert_setup(symbol: str, scored: dict) -> None:
    """Push an ntfy alert + log a registry event for a high-scoring setup.
    Reuses the token-manager ntfy channel; best-effort, never raises."""
    try:
        from app.core import registry
        registry.record_event(
            "info", "scanner",
            f"setup {symbol} {scored.get('bias')} score={scored.get('score')}: "
            + "; ".join(scored.get("reasons") or []))
    except Exception:
        pass
    try:
        import os

        import requests
        topic = os.environ.get("NTFY_TOPIC")
        if not topic:
            return
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=(f"{symbol} {scored.get('bias')} setup "
                  f"({scored.get('score')}): "
                  + ", ".join((scored.get('reasons') or [])[:3])).encode("utf-8"),
            headers={"Title": f"Scanner: {symbol}", "Priority": "default",
                     "Tags": "chart_with_upwards_trend"},
            timeout=5)
    except Exception:
        pass


def index_bias(metrics: dict, constituents: dict) -> dict:
    """Constituent-weighted directional bias for an index from its members'
    Tier-1 reads. Two weighted breadth signals, averaged into [-1, 1]:
      * buildup_breadth — net weighted positioning (long buildup / short
        covering = +1 per member; short buildup / long unwinding = -1)
      * price_breadth  — weighted momentum (clamped % move, ±3% = full weight)
    Positive = bullish. Only members present in `metrics` count; `coverage` is
    the summed weight actually seen, so a thin sweep is visible, not hidden."""
    total_w = bull_w = bear_w = 0.0
    buildup_acc = price_acc = 0.0
    contributors = []
    for sym, w in constituents.items():
        m = metrics.get(sym)
        if not m:
            continue
        b = m.get("buildup")
        direction = (1 if b in ("long_buildup", "short_covering")
                     else -1 if b in ("short_buildup", "long_unwinding") else 0)
        pc = m.get("price_change_pct") or 0.0
        total_w += w
        if direction > 0:
            bull_w += w
        elif direction < 0:
            bear_w += w
        buildup_acc += w * direction
        price_acc += w * max(-1.0, min(1.0, pc / 3.0))
        contributors.append({"symbol": sym, "weight": w, "buildup": b,
                             "price_change_pct": pc})
    if total_w == 0:
        return {"score": None, "buildup_breadth": None, "price_breadth": None,
                "bull_weight": 0.0, "bear_weight": 0.0, "coverage": 0.0, "n": 0,
                "label": "neutral", "contributors": []}
    buildup_breadth = buildup_acc / total_w
    price_breadth = price_acc / total_w
    score = round((buildup_breadth + price_breadth) / 2.0, 3)
    contributors.sort(key=lambda c: c["weight"], reverse=True)
    return {
        "score": score,
        "buildup_breadth": round(buildup_breadth, 3),
        "price_breadth": round(price_breadth, 3),
        "bull_weight": round(bull_w, 1), "bear_weight": round(bear_w, 1),
        "coverage": round(total_w, 1), "n": len(contributors),
        "label": _bias_label(score),
        "contributors": contributors[:8],
    }


def score_bias_day(bias_rows: list, spot_bars: list, horizon_min: int = 30):
    """Accuracy of a day's recorded bias vs the realized index move.
    For each bias reading, find the index spot `horizon_min` later and check
    whether the move's sign matched the bias sign (neutral readings skipped).
    `bias_rows`: (ts, score, spot); `spot_bars`: (ts, close) ascending.
    Returns (n, hits, avg_forward_move_pct). Pure — the nightly job feeds it
    from the store, a test feeds it literals."""
    if not bias_rows or not spot_bars:
        return (0, 0, 0.0)
    bars = [(t, c) for t, c in spot_bars if c is not None]
    if not bars:
        return (0, 0, 0.0)
    n = hits = 0
    move_sum = 0.0
    for ts, score, spot in bias_rows:
        if score is None or abs(score) < 0.3:
            continue                       # only score directional calls
        base = spot
        if base is None:                   # fall back to the nearest spot bar
            base = min(bars, key=lambda b: abs((b[0] - ts).total_seconds()))[1]
        target_t = ts + timedelta(minutes=horizon_min)
        future = [b for b in bars if b[0] >= target_t]
        if not future or not base:
            continue
        fwd = (future[0][1] - base) / base * 100.0
        n += 1
        move_sum += fwd
        if (score > 0 and fwd > 0) or (score < 0 and fwd < 0):
            hits += 1
    return (n, hits, round(move_sum / n, 3) if n else 0.0)


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
        self.scores: dict[str, dict] = {}      # symbol -> latest setup_score()
        self._prev_chain: dict[str, dict] = {} # symbol -> last chain cache (OI shift)
        self._alerted: dict[str, str] = {}     # symbol -> day already alerted
        self.index_bias: dict[str, dict] = {}  # index -> latest bias reading
        self.trader = None                     # optional ScannerTrader (positional book)

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
        self._record_index_bias(ts)
        return len(snaps)

    def _record_index_bias(self, ts) -> None:
        """Aggregate the fresh Tier-1 metrics into NIFTY/BANKNIFTY bias and
        persist a reading (with the index spot for later accuracy scoring)."""
        for index, weights in INDEX_CONSTITUENTS.items():
            bias = index_bias(self.metrics, weights)
            if bias.get("score") is None:
                continue
            try:
                bias["spot"] = self.store.latest_spot(index, ts.date())
            except Exception:
                bias["spot"] = None
            self.index_bias[index] = {**bias, "ts": str(ts)}
            try:
                self.store.upsert_index_bias(ts, index, bias)
            except Exception:
                pass

    def score_yesterday_bias(self, day, horizon_min: int = 30) -> None:
        """Nightly: score each index's recorded bias for `day` vs the realized
        index move `horizon_min` later, and persist the hit-rate. Needs a real
        store with index spot bars."""
        if not hasattr(self.store, "con"):
            return
        from app.core import registry
        for index in INDEX_CONSTITUENTS:
            bias_rows = self.store.index_bias_on(day, index)
            spot_bars = self.store.spot_bars_on(index, day)
            n, hits, avg_move = score_bias_day(bias_rows, spot_bars, horizon_min)
            if n:
                self.store.upsert_index_bias_accuracy(
                    day, index, horizon_min, n, hits, avg_move)
                registry.record_event(
                    "info", "scanner",
                    f"bias accuracy {index} {day}: {hits}/{n} "
                    f"({round(100 * hits / n)}%) @ {horizon_min}min")

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
        # Poll shortlisted names AND any open-position names (their chains must
        # stay live for MTM/exit even after they drop off the shortlist).
        held = self.trader.held_symbols() if self.trader else []
        want = list(dict.fromkeys([d["symbol"] for d in self.shortlist] + held))
        symbols = self._register_chain_cfgs(want)
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
        # Persist full chains to disk. By default this is limited to names the
        # trader actually holds (needed for MTM/exit reconstruction); every
        # shortlisted name is already scored/alerted above from the in-memory
        # cache regardless. Set scanner_record_chains=on to persist the whole
        # daily shortlist as a research/validation dataset instead.
        if registry.setting("scanner_record_chains", "off") == "on":
            to_persist = set(symbols)
        else:
            to_persist = set(symbols) & set(held)
        if to_persist and hasattr(self.store, "upsert_chain_rows"):
            try:
                hub.persist_chain_full(self.store, underlyings=to_persist)
            except Exception as e:
                registry.record_event("warn", "scanner", f"tier2 persist: {e!r}")
        # score every shortlisted name (Tier-1 read + Tier-2 chain) and alert
        # once/day per name above the settings threshold.
        try:
            threshold = float(registry.setting("scanner_alert_score", "70"))
        except (TypeError, ValueError):
            threshold = 70.0
        try:
            flag_at = float(registry.setting("scanner_flag_score", "55"))
        except (TypeError, ValueError):
            flag_at = 55.0
        today = datetime.now(IST).date().isoformat()
        ts_now = datetime.now(IST).replace(tzinfo=None)
        for d in self.shortlist:
            sym = d["symbol"]
            sc = setup_score(self.metrics.get(sym, {"symbol": sym}),
                             self.tier2.get(sym))
            self.scores[sym] = sc
            # record every above-flag setup with its entry premium so its
            # forward return can be measured later (validation, not trading).
            if sc["score"] >= flag_at and sc.get("bias"):
                self._record_flag(hub, sym, sc, ts_now)
            if sc["score"] >= threshold and self._alerted.get(sym) != today:
                self._alerted[sym] = today
                alert_setup(sym, sc)
        # positional paper trader acts on the fresh scores + live chains
        if self.trader:
            try:
                self.trader.step(hub, self)
            except Exception as e:
                registry.record_event("warn", "scanner", f"trader step: {e!r}")
        if done:
            registry.record_event("info", "scanner", f"tier-2 deep-dive: {done} names")
        return done

    def ranked_scores(self) -> list[dict]:
        """All current setup scores, highest first — backs GET /scanner. Falls
        back to a Tier-1-only score for names not yet deep-dived."""
        out = []
        seen = set()
        for sym, sc in self.scores.items():
            out.append(sc)
            seen.add(sym)
        for sym, m in self.metrics.items():
            if sym not in seen:
                out.append(setup_score(m, None))
        out.sort(key=lambda s: s.get("score") or 0, reverse=True)
        return out

    def detail(self, symbol: str) -> dict:
        """Full Tier-1 + Tier-2 picture for one symbol — backs GET
        /scanner/{symbol}."""
        return {
            "symbol": symbol,
            "score": self.scores.get(symbol),
            "tier1": self.metrics.get(symbol),
            "tier2": self.tier2.get(symbol),
            "universe": self._universe.get(symbol),
        }

    def _record_flag(self, hub, sym: str, sc: dict, ts) -> None:
        """Persist a flagged setup + the bias-side ATM premium at flag time."""
        side = "CALL" if sc.get("bias") == "CE" else "PUT"
        cache = hub._chain_cache.get(sym) or {}
        entry = None
        for (kind, off, soff, otype), q in cache.items():
            if soff == 0 and otype == side:
                entry = q.ltp
                break
        try:
            self.store.record_setup_flag(
                ts, sym, sc.get("bias"), sc.get("score"),
                self.metrics.get(sym, {}).get("spot"), entry)
        except Exception:
            pass

    def validate(self, since_day, horizon_min: int = 30) -> dict:
        """Forward-return hit-rate of setups flagged since `since_day`. For each
        flag, read the bias-side ATM premium `horizon_min` later from
        chain_snapshots and compare to entry. Needs a real store."""
        if not hasattr(self.store, "con"):
            return {"overall": {"n": 0}, "by_score": {}, "horizon_min": horizon_min}
        rows = []
        for f in self.store.setup_flags_since(since_day):
            entry = f.get("atm_premium")
            if entry in (None, 0):
                continue
            side = "CALL" if f.get("bias") == "CE" else "PUT"
            exit_ts = f["ts"] + timedelta(minutes=horizon_min)
            exit_px = self.store.atm_premium_at(f["symbol"], exit_ts, side)
            rows.append({"score": f.get("score"), "entry": entry, "exit": exit_px})
        return {**hitrate_stats(rows), "horizon_min": horizon_min,
                "flags": len(rows)}

    # -- signal provider (F6): the door strategies reach via ctx.signal() -----
    def signal_for(self, underlying: str, name: str):
        """Current scanner read of `name` for `underlying`. Returns None when
        unavailable so a strategy treats it as 'unknown'."""
        if name == "index_bias":
            return self.index_bias.get(underlying)
        if name == "setup":
            return self.scores.get(underlying)
        if name in ("tier1", "metrics"):
            return self.metrics.get(underlying)
        if name in ("tier2", "chain"):
            return self.tier2.get(underlying)
        return None

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
