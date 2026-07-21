#!/usr/bin/env python3
"""Did the options data see it coming? — event-window signal report.

Post-hoc read of the recorded chain footprint (chain_snapshots) and index
bias (index_bias_history) around specific intraday timestamps ("big move at
12:30 & 1:00" style questions). For each underlying x event time, prints:

  1. Spot path through the window (from underlying_bars).
  2. A PCR / ATM-IV / IV-skew / max-pain timeline sampled every `--sample`
     minutes from `--before` minutes ahead of the event to `--after` minutes
     after it — via chain_cache_asof + scanner.chain_metrics/max_pain, the
     SAME pure functions the live scanner and backtest replay use, so this
     reads exactly what the app would have seen in real time.
  3. The strikes with the biggest OI swing in the window (top --top-strikes
     by |delta|, split CALL/PUT) — the "where did positioning build" view.
  4. The index_bias reading (if recorded) just before vs at the event.

READ-ONLY — never touches the registry or the store. Run ON THE VPS where the
real recording lives; a dev container with no chain_snapshots will just
report "no chain data recorded" for every window (nothing crashes).

Usage:
    venv/bin/python -m scripts.event_window_signals
    venv/bin/python -m scripts.event_window_signals --date 2026-07-21 \\
        --underlying NIFTY BANKNIFTY --events 12:30 13:00 \\
        --before 20 --after 10 --sample 5 --top-strikes 8
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data.store import get_store            # noqa: E402
from app.engines import scanner                  # noqa: E402

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


def report_window(store, underlying: str, event: datetime, before: int,
                  after: int, sample: int, top_strikes: int, max_age_min: int):
    print(f"\n{'=' * 70}\n{underlying} @ {event.strftime('%H:%M')} "
          f"(window -{before}m / +{after}m)\n{'=' * 70}")

    win_start, win_end = event - timedelta(minutes=before), event + timedelta(minutes=after)
    spot = _spot_path(store, underlying, win_start, win_end)
    if spot:
        print(f"Spot:  open {spot['open']:.1f} -> close {spot['close']:.1f} "
              f"(move {spot['move']:+.1f}, range {spot['low']:.1f}-{spot['high']:.1f}, "
              f"{spot['bars']} bars)")
    else:
        print("Spot:  no underlying_bars recorded for this window")

    offsets = list(range(-before, after + 1, sample))
    if 0 not in offsets:
        offsets.append(0)
        offsets.sort()

    print(f"\n{'time':>6}  {'PCR(oi)':>8}  {'ATM IV':>7}  {'IV skew':>8}  "
          f"{'max pain':>9}  {'call OI':>10}  {'put OI':>10}")
    caches_by_offset = {}
    any_data = False
    for off in offsets:
        ts = event + timedelta(minutes=off)
        cache = _cache_at(store, underlying, ts, max_age_min)
        caches_by_offset[off] = cache
        if cache is None:
            print(f"{off:+5d}m  {'—':>8}  {'—':>7}  {'—':>8}  {'—':>9}  {'—':>10}  {'—':>10}")
            continue
        any_data = True
        m = scanner.chain_metrics(cache)
        mp = scanner.max_pain(cache)
        print(f"{off:+5d}m  {_fmt(m['pcr_oi'], 3):>8}  {_fmt(m['atm_iv'], 2):>7}  "
              f"{_fmt(m['iv_skew'], 2):>8}  {_fmt(mp, 0):>9}  "
              f"{_fmt(m['call_oi'], 0):>10}  {_fmt(m['put_oi'], 0):>10}")

    if not any_data:
        print("\n(no chain_snapshots recorded near this window — nothing to read)")
    else:
        before_cache = caches_by_offset.get(-before) or next(
            (c for o, c in sorted(caches_by_offset.items()) if c is not None), None)
        at_cache = caches_by_offset.get(0) or next(
            (c for o, c in sorted(caches_by_offset.items(), reverse=True) if c is not None), None)
        moves = _top_oi_moves(before_cache, at_cache, top_strikes)
        if moves:
            print(f"\nTop OI swings ({-before:+d}m -> event):")
            print(f"{'strike':>9}  {'type':>4}  {'delta OI':>10}  {'before':>10}  {'at event':>10}")
            for strike, otype, delta, ob, oa in moves:
                print(f"{strike:>9.0f}  {otype:>4}  {delta:>+10.0f}  "
                      f"{_fmt(ob, 0):>10}  {_fmt(oa, 0):>10}")

    ib_before = store.index_bias_asof(underlying, event - timedelta(minutes=before),
                                      max_age_min=max_age_min)
    ib_at = store.index_bias_asof(underlying, event, max_age_min=max_age_min)
    print(f"\nIndex bias:  before={_describe_bias(ib_before)}   "
          f"at-event={_describe_bias(ib_at)}")


def _describe_bias(ib):
    if not ib:
        return "no reading"
    return f"{ib['label']} (score {ib['score']:+.2f}, as-of {ib['as_of']})"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", type=date.fromisoformat, default=date.today(),
                    help="session date, YYYY-MM-DD (default: today, server-local)")
    ap.add_argument("--underlying", nargs="+", default=["NIFTY", "BANKNIFTY"])
    ap.add_argument("--events", nargs="+", default=["12:30", "13:00"],
                    help="event times, HH:MM (24h, IST)")
    ap.add_argument("--before", type=int, default=20, help="minutes before the event to start")
    ap.add_argument("--after", type=int, default=10, help="minutes after the event to end")
    ap.add_argument("--sample", type=int, default=5, help="minutes between metric samples")
    ap.add_argument("--top-strikes", type=int, default=8)
    ap.add_argument("--max-age-min", type=int, default=10,
                    help="reject a chain/bias reading older than this many minutes")
    args = ap.parse_args()

    store = get_store()
    if store.__class__.__name__ == "SyntheticStore":
        print("WARNING: no real market-data store found (or it's empty) — "
              "falling back to SyntheticStore. Run this on the VPS for real numbers.\n")

    for u in args.underlying:
        for ev in args.events:
            hh, mm = (int(x) for x in ev.split(":"))
            event_dt = datetime.combine(args.date, datetime.min.time()).replace(hour=hh, minute=mm)
            report_window(store, u, event_dt, args.before, args.after,
                         args.sample, args.top_strikes, args.max_age_min)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
