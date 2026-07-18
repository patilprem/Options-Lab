"""
Indicator & Price-Action Library (injected into every strategy)
===============================================================
A curated, unit-tested toolbox so LLM-generated strategies stop re-deriving
EMA / ATR / Supertrend / VWAP / pivots by hand (each hand-roll is a fresh
chance to get the formula wrong, and it fights the "keep per-bar work light"
rule). Available in strategy code as `indicators` — NO import needed, exactly
like `Strategy`, `LegSpec`, `math`.

Design contract:
  * Every function takes `bars` — a list of Bar-like objects (what
    ctx.history(n) returns: .ts .open .high .low .close .volume), oldest first
    — and reads a bounded window off the tail. Recomputing from the window each
    bar is O(window), not O(all history): cheap and stateless, so a strategy
    holds no indicator state and a restart can't desync it.
  * Insufficient data → None (never an exception). Strategies guard on None.
  * Session-aware helpers (vwap, prev_day, opening_range, cpr, pivots-from-
    history) group by calendar date, so pass them a multi-session history
    (use warmup_bars so the current session isn't the only one present).

Pure: depends only on `math` / `statistics`. Tested in tests/test_indicators.py
against hand-verified values and identities.
"""

from __future__ import annotations

import math
import statistics
from typing import Optional


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _series(bars, key: str) -> list:
    return [getattr(b, key) for b in bars]


def _closes(bars) -> list:
    return [b.close for b in bars]


def true_ranges(bars) -> list:
    """Per-bar True Range series (len = len(bars)-1; first bar has no prev)."""
    out = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
        out.append(max(h - l, abs(h - pc), abs(l - pc)))
    return out


def _wilder(values: list, n: int) -> Optional[float]:
    """Wilder's smoothed average of `values` with period n (seed = simple mean
    of the first n, then RMA). None if fewer than n values."""
    if len(values) < n or n <= 0:
        return None
    avg = sum(values[:n]) / n
    for v in values[n:]:
        avg = (avg * (n - 1) + v) / n
    return avg


# --------------------------------------------------------------------------
# moving averages / momentum
# --------------------------------------------------------------------------

def sma(bars, n: int, key: str = "close") -> Optional[float]:
    s = _series(bars, key)
    if len(s) < n or n <= 0:
        return None
    return sum(s[-n:]) / n


def ema(bars, n: int, key: str = "close") -> Optional[float]:
    """Exponential MA (alpha = 2/(n+1)), seeded with the SMA of the first n
    values then iterated over the rest of the supplied window. Deeper history
    (warmup) → a more settled value; needs at least n bars."""
    s = _series(bars, key)
    if len(s) < n or n <= 0:
        return None
    k = 2.0 / (n + 1)
    e = sum(s[:n]) / n
    for v in s[n:]:
        e = v * k + e * (1 - k)
    return e


def rsi(bars, n: int = 14) -> Optional[float]:
    """Wilder RSI over the supplied window. Needs > n bars. Returns 0..100."""
    s = _closes(bars)
    if len(s) <= n or n <= 0:
        return None
    gains, losses = [], []
    for i in range(1, len(s)):
        d = s[i] - s[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = _wilder(gains, n)
    avg_loss = _wilder(losses, n)
    if avg_loss == 0:
        return 100.0 if (avg_gain or 0) > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def macd(bars, fast: int = 12, slow: int = 26, signal: int = 9) -> Optional[dict]:
    """MACD line / signal / histogram. Needs slow+signal bars for a settled
    signal line. Returns {"macd", "signal", "hist"} or None."""
    s = _closes(bars)
    if len(s) < slow + signal:
        return None
    # rolling EMA series for the macd line, then EMA of that for the signal
    def _ema_series(vals, p):
        k = 2.0 / (p + 1)
        e = sum(vals[:p]) / p
        seq = [e]
        for v in vals[p:]:
            e = v * k + e * (1 - k)
            seq.append(e)
        return seq
    fast_e = _ema_series(s, fast)
    slow_e = _ema_series(s, slow)
    # align tails (slow_e is shorter)
    m = min(len(fast_e), len(slow_e))
    macd_line = [fast_e[-m + i] - slow_e[-m + i] for i in range(m)]
    if len(macd_line) < signal:
        return None
    sig = _ema_series(macd_line, signal)[-1]
    line = macd_line[-1]
    return {"macd": line, "signal": sig, "hist": line - sig}


# --------------------------------------------------------------------------
# volatility / trend
# --------------------------------------------------------------------------

def atr(bars, n: int = 14) -> Optional[float]:
    """Average True Range (Wilder). Needs n+1 bars."""
    tr = true_ranges(bars)
    return _wilder(tr, n)


def bollinger(bars, n: int = 20, k: float = 2.0, key: str = "close") -> Optional[dict]:
    """Bollinger Bands. Returns {"mid","upper","lower","width"} or None.
    width = (upper-lower)/mid (a %B-style squeeze gauge)."""
    s = _series(bars, key)
    if len(s) < n or n <= 0:
        return None
    window = s[-n:]
    mid = sum(window) / n
    sd = statistics.pstdev(window)
    upper, lower = mid + k * sd, mid - k * sd
    return {"mid": mid, "upper": upper, "lower": lower,
            "width": (upper - lower) / mid if mid else 0.0}


def adx(bars, n: int = 14) -> Optional[dict]:
    """Wilder ADX with +DI / -DI. Needs ~2n+1 bars for a settled value.
    Returns {"adx","plus_di","minus_di"} or None."""
    if len(bars) < 2 * n + 1:
        return None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(bars)):
        up = bars[i].high - bars[i - 1].high
        dn = bars[i - 1].low - bars[i].low
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    # smoothed DI series, then ADX = Wilder-smoothed DX
    def _smooth_seq(vals):
        seq = []
        s = sum(vals[:n])
        seq.append(s)
        for v in vals[n:]:
            s = s - s / n + v
            seq.append(s)
        return seq
    tr_s = _smooth_seq(trs)
    pdm_s = _smooth_seq(plus_dm)
    mdm_s = _smooth_seq(minus_dm)
    dx = []
    for tr_v, pdm_v, mdm_v in zip(tr_s, pdm_s, mdm_s):
        if tr_v == 0:
            dx.append(0.0)
            continue
        pdi = 100.0 * pdm_v / tr_v
        mdi = 100.0 * mdm_v / tr_v
        denom = pdi + mdi
        dx.append(100.0 * abs(pdi - mdi) / denom if denom else 0.0)
    adx_val = _wilder(dx, n)
    if adx_val is None:
        return None
    tr_last, pdm_last, mdm_last = tr_s[-1], pdm_s[-1], mdm_s[-1]
    plus_di = 100.0 * pdm_last / tr_last if tr_last else 0.0
    minus_di = 100.0 * mdm_last / tr_last if tr_last else 0.0
    return {"adx": adx_val, "plus_di": plus_di, "minus_di": minus_di}


def supertrend(bars, period: int = 10, mult: float = 3.0) -> Optional[dict]:
    """Supertrend. Returns {"dir": +1 up / -1 down, "level": line value} or
    None. Recomputed from the window each call (no carried state)."""
    if len(bars) < period + 1:
        return None
    tr = true_ranges(bars)                       # len = len(bars)-1
    # ATR series (Wilder) aligned so atr_seq[i] corresponds to bars[i+1]
    atr_seq = []
    a = sum(tr[:period]) / period
    atr_seq.append(a)
    for v in tr[period:]:
        a = (a * (period - 1) + v) / period
        atr_seq.append(a)
    # bars covered by atr_seq start at index `period`
    start = period
    st_dir = 1
    st_level = None
    fub = flb = None
    for j, a in enumerate(atr_seq):
        i = start + j
        mid = (bars[i].high + bars[i].low) / 2.0
        ub, lb = mid + mult * a, mid - mult * a
        close = bars[i].close
        prev_close = bars[i - 1].close
        fub = ub if (fub is None or ub < fub or prev_close > fub) else fub
        flb = lb if (flb is None or lb > flb or prev_close < flb) else flb
        if st_level is None:
            st_dir = 1 if close >= mid else -1
        elif st_dir == 1:
            st_dir = -1 if close < flb else 1
        else:
            st_dir = 1 if close > fub else -1
        st_level = flb if st_dir == 1 else fub
    return {"dir": st_dir, "level": st_level}


# --------------------------------------------------------------------------
# volume
# --------------------------------------------------------------------------

def vwap(bars) -> Optional[float]:
    """Session-anchored VWAP over the LATEST session in `bars` (grouped by
    date). On indexes (all volume 0) it degrades to the average typical price,
    so it stays usable as a mean-reference line. None if no bars."""
    if not bars:
        return None
    day = bars[-1].ts.date()
    sess = [b for b in bars if b.ts.date() == day]
    pv = vol = 0.0
    for b in sess:
        tp = (b.high + b.low + b.close) / 3.0
        w = (b.volume or 0.0)
        pv += tp * w
        vol += w
    if vol > 0:
        return pv / vol
    return sum((b.high + b.low + b.close) / 3.0 for b in sess) / len(sess)


# --------------------------------------------------------------------------
# price-action structure
# --------------------------------------------------------------------------

def range_position(bar) -> Optional[float]:
    """Where the close sits within a bar's range: 0 = at the low, 1 = at the
    high. None for a zero-range bar."""
    rng = bar.high - bar.low
    if rng <= 0:
        return None
    return (bar.close - bar.low) / rng


def is_inside_bar(bars) -> bool:
    """Last bar's range is inside the prior bar's (compression)."""
    if len(bars) < 2:
        return False
    a, b = bars[-2], bars[-1]
    return b.high <= a.high and b.low >= a.low


def is_outside_bar(bars) -> bool:
    """Last bar engulfs the prior bar's range (expansion)."""
    if len(bars) < 2:
        return False
    a, b = bars[-2], bars[-1]
    return b.high >= a.high and b.low <= a.low


def swing_high(bars, left: int = 2, right: int = 2) -> Optional[float]:
    """Most recent confirmed swing-high price (a bar whose high exceeds `left`
    bars before and `right` bars after it). None if none in the window."""
    return _last_swing(bars, left, right, high=True)


def swing_low(bars, left: int = 2, right: int = 2) -> Optional[float]:
    return _last_swing(bars, left, right, high=False)


def _last_swing(bars, left: int, right: int, high: bool):
    n = len(bars)
    for i in range(n - right - 1, left - 1, -1):
        piv = bars[i].high if high else bars[i].low
        ok = True
        for j in range(i - left, i + right + 1):
            if j == i:
                continue
            v = bars[j].high if high else bars[j].low
            if (high and v >= piv) or (not high and v <= piv):
                ok = False
                break
        if ok:
            return piv
    return None


def break_of_structure(bars, lookback: int = 20, left: int = 2, right: int = 2):
    """Detect a break of the most recent swing by the latest close.
    Returns "up" (close above last swing high), "down" (below last swing low),
    or None. Uses the last `lookback` bars."""
    window = bars[-lookback:] if len(bars) > lookback else bars
    if len(window) < left + right + 2:
        return None
    close = window[-1].close
    sh = _last_swing(window[:-1], left, right, high=True)
    sl = _last_swing(window[:-1], left, right, high=False)
    if sh is not None and close > sh:
        return "up"
    if sl is not None and close < sl:
        return "down"
    return None


# --------------------------------------------------------------------------
# session references & pivots
# --------------------------------------------------------------------------

def _sessions(bars) -> dict:
    """Group bars by calendar date -> list, preserving order."""
    out: dict = {}
    for b in bars:
        out.setdefault(b.ts.date(), []).append(b)
    return out


def prev_day(bars) -> Optional[dict]:
    """OHLC of the session immediately before the latest one in `bars`.
    None if `bars` spans only one session. Needs warmup/multi-day history."""
    sess = _sessions(bars)
    days = sorted(sess)
    if len(days) < 2:
        return None
    p = sess[days[-2]]
    return {"open": p[0].open, "high": max(b.high for b in p),
            "low": min(b.low for b in p), "close": p[-1].close}


def opening_range(bars, minutes: int = 15) -> Optional[dict]:
    """High/low of the first `minutes` of the LATEST session in `bars`.
    Returns {"high","low"} or None if the opening window isn't present yet."""
    if not bars:
        return None
    day = bars[-1].ts.date()
    sess = [b for b in bars if b.ts.date() == day]
    if not sess:
        return None
    start = sess[0].ts
    window = [b for b in sess
              if (b.ts - start).total_seconds() < minutes * 60]
    if not window:
        return None
    return {"high": max(b.high for b in window),
            "low": min(b.low for b in window)}


def pivots(high: float, low: float, close: float) -> dict:
    """Classic floor-trader pivots from a prior session's H/L/C.
    Returns pivot P and three support/resistance levels."""
    p = (high + low + close) / 3.0
    return {"p": p, "r1": 2 * p - low, "s1": 2 * p - high,
            "r2": p + (high - low), "s2": p - (high - low),
            "r3": high + 2 * (p - low), "s3": low - 2 * (high - p)}


def cpr(high: float, low: float, close: float) -> dict:
    """Central Pivot Range from a prior session's H/L/C.
    Returns {"pivot","bc","tc","width"} with tc>=bc. A narrow width forecasts
    a trending day; wide forecasts range-bound."""
    pivot = (high + low + close) / 3.0
    bc = (high + low) / 2.0
    tc = 2 * pivot - bc
    if tc < bc:
        tc, bc = bc, tc
    return {"pivot": pivot, "bc": bc, "tc": tc, "width": abs(tc - bc)}


def pivots_from_history(bars) -> Optional[dict]:
    """Convenience: floor pivots for the latest session computed from the
    PRIOR session in `bars` (needs multi-day history). None otherwise."""
    pd = prev_day(bars)
    if pd is None:
        return None
    return pivots(pd["high"], pd["low"], pd["close"])


def percentile_rank(value, series) -> Optional[float]:
    """Percentile rank (0..100) of `value` within `series`: the % of samples at
    or below it. Backs ctx.iv_rank() — "is today's ATM IV rich vs its recent
    range?". None if `value` is None or `series` is empty."""
    if value is None or not series:
        return None
    below = sum(1 for x in series if x <= value)
    return 100.0 * below / len(series)


def gap_pct(bars) -> Optional[float]:
    """Opening gap of the latest session vs the prior session's close, in %.
    Positive = gap up. None without two sessions."""
    sess = _sessions(bars)
    days = sorted(sess)
    if len(days) < 2:
        return None
    prev_close = sess[days[-2]][-1].close
    today_open = sess[days[-1]][0].open
    if not prev_close:
        return None
    return 100.0 * (today_open - prev_close) / prev_close
