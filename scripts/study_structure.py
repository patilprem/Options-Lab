#!/usr/bin/env python3
"""Structure-reaction study — puts numbers on the chart observations of
2026-06/07 (19 hand-annotated days) using the FULL recorded history.

Hypotheses under test:
  H1  Wide CPR (prev day closed at its extreme) -> chop / "no-trade" day.
  H2  Pivot-recross count by 11:00 is a live chop detector.
  H3  Extension fade: price touching a static level while stretched
      >= K ATRs from session VWAP reverts before it continues
      (vs a control group touching levels UNstretched).
  H4  First touch of a level reacts better than later touches.

Read-only. Run ON THE VPS:
  venv/bin/python scripts/study_structure.py               # full history
  venv/bin/python scripts/study_structure.py --from 2025-07-01
  venv/bin/python scripts/study_structure.py --k 1.5 --race-pts 20
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import mean, median

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data.store import get_store  # noqa: E402

SESSION_START, SESSION_END = time(9, 15), time(15, 30)
ENTRY_FROM, ENTRY_TO = time(9, 50), time(14, 30)


# ---------------------------------------------------------------------------
# data prep
# ---------------------------------------------------------------------------

def load_days(store, underlying, d_from, d_to):
    """{date: [Bar,...]} for full sessions (>=50 5-min bars)."""
    bars = store.underlying_bars(underlying, datetime.combine(d_from, SESSION_START),
                                 datetime.combine(d_to, SESSION_END), 5)
    days: dict[date, list] = defaultdict(list)
    for b in bars:
        if SESSION_START <= b.ts.time() <= SESSION_END:
            days[b.ts.date()].append(b)
    return {d: bs for d, bs in sorted(days.items()) if len(bs) >= 50}


def day_ohlc(bars):
    return (bars[0].open, max(b.high for b in bars),
            min(b.low for b in bars), bars[-1].close)


def cpr_levels(prev_ohlc):
    """Classic floor-trader levels from the previous session."""
    _, h, l, c = prev_ohlc
    p = (h + l + c) / 3.0
    bc = (h + l) / 2.0
    tc = 2 * p - bc
    return {"P": p, "TC": max(tc, bc), "BC": min(tc, bc),
            "R1": 2 * p - l, "S1": 2 * p - h, "prevH": h, "prevL": l}


def session_series(bars):
    """Per-bar (vwap, atr) computed exactly like the strategies do."""
    out, pv, v, atr, prev_c = [], 0.0, 0.0, None, None
    for b in bars:
        w = b.volume if b.volume and b.volume > 0 else 1.0
        pv += w * (b.high + b.low + b.close) / 3.0
        v += w
        tr = b.high - b.low
        if prev_c is not None:
            tr = max(tr, abs(b.high - prev_c), abs(b.low - prev_c))
        atr = tr if atr is None else tr * (2.0 / 21) + atr * (19.0 / 21)
        prev_c = b.close
        out.append((pv / v, atr))
    return out


# ---------------------------------------------------------------------------
# per-day metrics (H1, H2)
# ---------------------------------------------------------------------------

def day_metrics(bars, levels):
    o, h, l, c = day_ohlc(bars)
    rng = h - l
    trend_eff = abs(c - o) / rng if rng else 0.0
    p = levels["P"]
    recross_all = recross_11 = 0
    prev_side = None
    for b in bars:
        side = b.close >= p
        if prev_side is not None and side != prev_side:
            recross_all += 1
            if b.ts.time() <= time(11, 0):
                recross_11 += 1
        prev_side = side
    return {"range": rng, "trend_eff": trend_eff,
            "recross_all": recross_all, "recross_11": recross_11}


# ---------------------------------------------------------------------------
# event scan (H3, H4)
# ---------------------------------------------------------------------------

def race(bars, i, direction, pts, horizon):
    """From bar i's close: which comes first within `horizon` bars —
    reversion by `pts` (against `direction`) or continuation by `pts`?"""
    c = bars[i].close
    for b in bars[i + 1:i + 1 + horizon]:
        rev = (b.low <= c - pts) if direction > 0 else (b.high >= c + pts)
        con = (b.high >= c + pts) if direction > 0 else (b.low <= c - pts)
        if rev and con:
            return "both"          # one bar swept both — ambiguous, excluded
        if rev:
            return "revert"
        if con:
            return "continue"
    return "neither"


def scan_events(bars, levels, series, k, tol_pts, race_pts, horizon):
    """Yield (kind, event) where kind is 'fade' (stretched at level) or
    'control' (at level, unstretched). Direction: +1 price stretched up."""
    touched: set[str] = set()
    lv = sorted(levels.items(), key=lambda kv: kv[1])
    for i, b in enumerate(bars):
        t = b.ts.time()
        if not (ENTRY_FROM <= t <= ENTRY_TO):
            continue
        vwap, atr = series[i]
        if not atr:
            continue
        ext = (b.close - vwap) / atr
        for name, level in lv:
            hit = b.low - tol_pts <= level <= b.high + tol_pts
            if not hit:
                continue
            first = name not in touched
            touched.add(name)
            direction = 1 if ext > 0 else -1
            # the level must oppose the stretch (a wall ahead, not behind)
            opposing = (level >= b.close - tol_pts) if direction > 0 \
                else (level <= b.close + tol_pts)
            if not opposing:
                continue
            outcome = race(bars, i, direction, race_pts, horizon)
            ev = {"time": t, "level": name, "first": first,
                  "ext": ext, "outcome": outcome}
            if abs(ext) >= k:
                yield "fade", ev
            elif abs(ext) < 1.0:
                yield "control", ev
            break   # one event per bar (nearest touched level)


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def pct(part, whole):
    return f"{100 * part / whole:5.1f}%" if whole else "   — "


def tercile_table(rows, key, label):
    """rows sorted by key -> 3 buckets, report day-type stats per bucket."""
    rows = sorted(rows, key=lambda r: r[key])
    n = len(rows)
    out = [f"\n{label} (n={n} days)"]
    out.append(f"{'bucket':<10}{'range(med)':>12}{'trend_eff(med)':>16}"
               f"{'recross11(med)':>16}{'chop days':>11}")
    for bi, name in enumerate(("narrow", "mid", "wide")):
        seg = rows[bi * n // 3:(bi + 1) * n // 3]
        if not seg:
            continue
        chop = sum(1 for r in seg if r["trend_eff"] < 0.35)
        out.append(f"{name:<10}{median(r['range'] for r in seg):>12.0f}"
                   f"{median(r['trend_eff'] for r in seg):>16.2f}"
                   f"{median(r['recross_11'] for r in seg):>16.0f}"
                   f"{pct(chop, len(seg)):>11}")
    return "\n".join(out)


def outcome_table(events, label):
    n = len(events)
    if not n:
        return f"\n{label}: no events"
    r = sum(1 for e in events if e["outcome"] == "revert")
    c = sum(1 for e in events if e["outcome"] == "continue")
    x = n - r - c
    return (f"\n{label}  (n={n})\n"
            f"  revert first : {r:4d}  {pct(r, r + c)} of decided\n"
            f"  continue     : {c:4d}  {pct(c, r + c)}\n"
            f"  undecided    : {x:4d}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--underlying", default="NIFTY")
    ap.add_argument("--from", dest="d_from", default=None)
    ap.add_argument("--to", dest="d_to", default=None)
    ap.add_argument("--k", type=float, default=2.0, help="extension ATRs")
    ap.add_argument("--tol", type=float, default=10.0, help="touch tolerance pts")
    ap.add_argument("--race-pts", type=float, default=25.0)
    ap.add_argument("--horizon", type=int, default=24, help="bars (24=2h)")
    a = ap.parse_args()

    store = get_store()
    if a.d_from and a.d_to:
        d_from, d_to = date.fromisoformat(a.d_from), date.fromisoformat(a.d_to)
    else:
        try:
            rows, _ = store.coverage()
            span = next(r for r in rows if r[0] == a.underlying)
            d_from = a.d_from and date.fromisoformat(a.d_from) or span[1].date()
            d_to = a.d_to and date.fromisoformat(a.d_to) or span[2].date()
        except Exception:
            d_to = date.today()
            d_from = d_to - timedelta(days=365)

    days = load_days(store, a.underlying, d_from, d_to)
    print(f"study: {a.underlying} {d_from} .. {d_to} — {len(days)} full sessions"
          f"  (k={a.k} ATR, touch±{a.tol}, race ±{a.race_pts} pts, "
          f"{a.horizon} bars)")
    if len(days) < 30:
        print("!! under 30 sessions — treat every number below as anecdote")

    dm_rows, fades, controls = [], [], []
    ordered = sorted(days)
    for prev_d, d in zip(ordered, ordered[1:]):
        levels = cpr_levels(day_ohlc(days[prev_d]))
        bars = days[d]
        m = day_metrics(bars, levels)
        m["cpr_width"] = levels["TC"] - levels["BC"]
        m["date"] = d
        dm_rows.append(m)
        series = session_series(bars)
        for kind, ev in scan_events(bars, levels, series, a.k, a.tol,
                                    a.race_pts, a.horizon):
            (fades if kind == "fade" else controls).append(ev)

    # H1 — CPR width vs day type
    print(tercile_table(dm_rows, "cpr_width",
                        "H1: day type by CPR width tercile"))

    # H2 — recross-by-11 as chop detector
    lo = [r for r in dm_rows if r["recross_11"] <= 1]
    hi = [r for r in dm_rows if r["recross_11"] >= 3]
    print(f"\nH2: pivot recrosses by 11:00 -> rest-of-day character")
    for name, seg in (("<=1 recross", lo), (">=3 recrosses", hi)):
        if seg:
            chop = sum(1 for r in seg if r["trend_eff"] < 0.35)
            print(f"  {name:<15} n={len(seg):3d}  trend_eff(med)="
                  f"{median(r['trend_eff'] for r in seg):.2f}  "
                  f"chop days={pct(chop, len(seg))}")

    # H3 — extension fade vs control
    print(outcome_table(fades, f"H3: FADE events (|ext|>={a.k} ATR at level)"))
    print(outcome_table(controls, "H3: CONTROL (at level, |ext|<1 ATR)"))

    # H4 — first touch vs later
    print(outcome_table([e for e in fades if e["first"]],
                        "H4: fades on FIRST touch of the level"))
    print(outcome_table([e for e in fades if not e["first"]],
                        "H4: fades on later touches"))

    # time-of-day profile of decided fade winners
    wins = [e for e in fades if e["outcome"] == "revert"]
    if wins:
        hrs = defaultdict(int)
        for e in wins:
            hrs[e["time"].hour] += 1
        prof = "  ".join(f"{h:02d}h:{n}" for h, n in sorted(hrs.items()))
        print(f"\nfade winners by hour: {prof}")


if __name__ == "__main__":
    main()
