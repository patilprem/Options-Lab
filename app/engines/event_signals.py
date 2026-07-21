"""Event-window signal report — did the options data see a move coming?

Post-hoc read of the recorded chain footprint (chain_snapshots) and index
bias (index_bias_history) around a specific intraday timestamp. Pure
functions over a store + scanner.chain_metrics/max_pain (the SAME functions
the live scanner and backtest replay use), so this reads exactly what the
app would have seen in real time. Used by both scripts/event_window_signals.py
(a thin CLI, for offline/ad-hoc runs) and the /data/event_window_signals API
endpoint (reuses the app's already-open store connection — the only way to
read this while the app is live, since DuckDB's file lock is exclusive
against a second process, readers included).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.engines import scanner

WEEKLY0 = ("WEEKLY", 0)


def _cache_at(store, underlying: str, ts: datetime, max_age_min: int):
    """Point-in-time chain cache restricted to the current weekly expiry —
    the most liquid, most-watched contract for a same-day move."""
    raw = store.chain_cache_asof(underlying, ts, max_age_min=max_age_min)
    if not raw:
        return None
    cache = {k: q for k, q in raw.items() if (k[0], k[1]) == WEEKLY0}
    return cache or None


def _oi_by_strike(cache) -> dict:
    if not cache:
        return {}
    return {(q.strike, k[3]): (q.oi or 0) for k, q in cache.items()
            if q.strike is not None}


def _top_oi_moves(before_cache, at_cache, top_n: int):
    b, a = _oi_by_strike(before_cache), _oi_by_strike(at_cache)
    keys = set(b) | set(a)
    rows = [(strike, otype, a.get((strike, otype), 0) - b.get((strike, otype), 0),
             b.get((strike, otype)), a.get((strike, otype)))
            for strike, otype in keys]
    rows.sort(key=lambda r: abs(r[2]), reverse=True)
    return rows[:top_n]


def _fmt(v, nd=2):
    return "—" if v is None else f"{v:.{nd}f}"


def _spot_path(store, underlying: str, start: datetime, end: datetime):
    bars = store.underlying_bars(underlying, start, end, interval_min=5)
    if not bars:
        return None
    return {
        "open": bars[0].open, "close": bars[-1].close,
        "high": max(b.high for b in bars), "low": min(b.low for b in bars),
        "move": bars[-1].close - bars[0].open, "bars": len(bars),
    }


def _describe_bias(ib):
    if not ib:
        return "no reading"
    return f"{ib['label']} (score {ib['score']:+.2f}, as-of {ib['as_of']})"


def build_report(store, underlying: str, event: datetime, before: int = 20,
                 after: int = 10, sample: int = 5, top_strikes: int = 8,
                 max_age_min: int = 10) -> str:
    """Full formatted text report for one underlying x event timestamp."""
    lines = [f"\n{'=' * 70}", f"{underlying} @ {event.strftime('%H:%M')} "
             f"(window -{before}m / +{after}m)", "=" * 70]

    win_start, win_end = event - timedelta(minutes=before), event + timedelta(minutes=after)
    spot = _spot_path(store, underlying, win_start, win_end)
    if spot:
        lines.append(f"Spot:  open {spot['open']:.1f} -> close {spot['close']:.1f} "
                     f"(move {spot['move']:+.1f}, range {spot['low']:.1f}-{spot['high']:.1f}, "
                     f"{spot['bars']} bars)")
    else:
        lines.append("Spot:  no underlying_bars recorded for this window")

    offsets = list(range(-before, after + 1, sample))
    if 0 not in offsets:
        offsets.append(0)
        offsets.sort()

    lines.append(f"\n{'time':>6}  {'PCR(oi)':>8}  {'ATM IV':>7}  {'IV skew':>8}  "
                 f"{'max pain':>9}  {'call OI':>10}  {'put OI':>10}")
    caches_by_offset = {}
    any_data = False
    for off in offsets:
        ts = event + timedelta(minutes=off)
        cache = _cache_at(store, underlying, ts, max_age_min)
        caches_by_offset[off] = cache
        if cache is None:
            lines.append(f"{off:+5d}m  {'—':>8}  {'—':>7}  {'—':>8}  {'—':>9}  {'—':>10}  {'—':>10}")
            continue
        any_data = True
        m = scanner.chain_metrics(cache)
        mp = scanner.max_pain(cache)
        lines.append(f"{off:+5d}m  {_fmt(m['pcr_oi'], 3):>8}  {_fmt(m['atm_iv'], 2):>7}  "
                     f"{_fmt(m['iv_skew'], 2):>8}  {_fmt(mp, 0):>9}  "
                     f"{_fmt(m['call_oi'], 0):>10}  {_fmt(m['put_oi'], 0):>10}")

    if not any_data:
        lines.append("\n(no chain_snapshots recorded near this window — nothing to read)")
    else:
        before_cache = caches_by_offset.get(-before) or next(
            (c for o, c in sorted(caches_by_offset.items()) if c is not None), None)
        at_cache = caches_by_offset.get(0) or next(
            (c for o, c in sorted(caches_by_offset.items(), reverse=True) if c is not None), None)
        moves = _top_oi_moves(before_cache, at_cache, top_strikes)
        if moves:
            lines.append(f"\nTop OI swings ({-before:+d}m -> event):")
            lines.append(f"{'strike':>9}  {'type':>4}  {'delta OI':>10}  {'before':>10}  {'at event':>10}")
            for strike, otype, delta, ob, oa in moves:
                lines.append(f"{strike:>9.0f}  {otype:>4}  {delta:>+10.0f}  "
                             f"{_fmt(ob, 0):>10}  {_fmt(oa, 0):>10}")

    ib_before = store.index_bias_asof(underlying, event - timedelta(minutes=before),
                                      max_age_min=max_age_min)
    ib_at = store.index_bias_asof(underlying, event, max_age_min=max_age_min)
    lines.append(f"\nIndex bias:  before={_describe_bias(ib_before)}   "
                f"at-event={_describe_bias(ib_at)}")
    return "\n".join(lines)
