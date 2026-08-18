"""Pre-move clue finder — what was the data doing BEFORE the move?

event_signals answers "what happened around 11:30?" — you have to already know
the time. This answers the question you actually have after a surprising
session: SOMETHING moved today; was there a tell, and what was it?

So the move is not an input. `find_moves` locates the day's sharpest directional
legs from underlying_bars, and for each one `clues` compares the run-up window
against the REST OF THE SAME SESSION — every chain metric, the index bias, and
the spot's own behaviour — and reports which ones were behaving abnormally
beforehand.

Two disciplines carried over from the insights engines, for the same reason:

  * Baseline-relative, never absolute. "PCR fell 0.06" means nothing on its own;
    "PCR fell 4.1 sigma faster than it moved all day" is a claim. Nothing here
    has a tuned threshold in it, so it doesn't need re-tuning per underlying.
  * Gated on sample count. A z-score off three samples is noise wearing a
    number's clothes. Below MIN_SAMPLES a metric reports 'insufficient', not a
    verdict — small-N noise must never become advice.

A clue found here is a HYPOTHESIS, not a signal. One day proves nothing: the
same sweep has to hold up across many sessions before it belongs in a gate.
PURE and READ-ONLY — pass a store, get a report.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.engines import scanner
from app.engines.event_signals import _cache_at

MIN_SAMPLES = 8          # below this a z-score is not evidence
MIN_PRE_SAMPLES = 3      # need at least this many readings in the run-up
Z_NOTABLE = 1.5          # |z| at or above this is worth printing
METRICS = ("pcr_oi", "atm_iv", "iv_skew", "call_oi", "put_oi", "max_pain")


# --------------------------------------------------------------------------
# Finding the move (so you don't have to know when it was)
# --------------------------------------------------------------------------

def find_moves(bars, window_min: int = 30, interval_min: int = 5, top_n: int = 3):
    """The day's sharpest directional legs, biggest first.

    Scans every `window_min` span and scores it by NET move (close-to-close),
    not range: a 200-point whipsaw that ends where it started is not the thing
    you are trying to explain. Overlapping windows are collapsed so one move
    is reported once rather than as five near-identical neighbours.
    """
    if not bars:
        return []
    span = max(1, window_min // max(1, interval_min))
    if len(bars) <= span:
        return []
    cands = []
    for i in range(len(bars) - span):
        a, b = bars[i], bars[i + span]
        cands.append({
            "start": a.ts, "end": b.ts,
            "from": a.close, "to": b.close,
            "move": b.close - a.close,
            "pct": ((b.close - a.close) / a.close * 100.0) if a.close else 0.0,
        })
    cands.sort(key=lambda c: abs(c["move"]), reverse=True)
    out = []
    for c in cands:
        if any(c["start"] < o["end"] and o["start"] < c["end"] for o in out):
            continue          # overlaps a bigger move already taken
        out.append(c)
        if len(out) >= top_n:
            break
    return out


def spot_stats(bars):
    """Per-bar |return| and range, for the spot's own pre-move behaviour.

    Volatility COMPRESSION before an expansion is one of the oldest tells
    there is, and it lives in the bars — no chain data required — so it is
    the one clue still available on a day the poller was down."""
    out = []
    for i, b in enumerate(bars):
        prev = bars[i - 1].close if i else b.open
        out.append({
            "ts": b.ts,
            "ret_abs": abs(b.close - prev),
            "range": b.high - b.low,
            "volume": b.volume or 0.0,
        })
    return out


# --------------------------------------------------------------------------
# Session-wide chain timeline
# --------------------------------------------------------------------------

def session_timeline(store, underlying: str, day, start_hm=(9, 15),
                     end_hm=(15, 30), sample: int = 5, max_age_min: int = 10):
    """Chain metrics sampled across the whole session.

    Expiry is PINNED to the front contract for the day (same discipline as
    event_signals.build_report): a sample whose freshest batch caught only the
    next expiry is dropped rather than silently compared against the front
    month's numbers.
    """
    base = datetime.combine(day, datetime.min.time())
    t = base.replace(hour=start_hm[0], minute=start_hm[1])
    end = base.replace(hour=end_hm[0], minute=end_hm[1])
    raw = []
    while t <= end:
        raw.append((t, _cache_at(store, underlying, t, max_age_min)))
        t += timedelta(minutes=sample)

    groups = {next(iter(c))[:2] for _, c in raw if c}
    from app.engines.event_signals import _KIND_RANK
    pinned = (min(groups, key=lambda g: (g[1], _KIND_RANK.get(g[0], 9)))
              if groups else None)

    rows, dropped = [], 0
    for ts, cache in raw:
        if cache and next(iter(cache))[:2] != pinned:
            cache, dropped = None, dropped + 1
        if not cache:
            rows.append({"ts": ts, **{m: None for m in METRICS}})
            continue
        m = scanner.chain_metrics(cache)
        rows.append({"ts": ts, "pcr_oi": m["pcr_oi"], "atm_iv": m["atm_iv"],
                     "iv_skew": m["iv_skew"], "call_oi": m["call_oi"],
                     "put_oi": m["put_oi"], "max_pain": scanner.max_pain(cache)})
    return {"rows": rows, "expiry": pinned, "dropped": dropped}


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def _mean_sd(xs):
    if len(xs) < 2:
        return (xs[0] if xs else None), None
    mu = sum(xs) / len(xs)
    var = sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)
    return mu, var ** 0.5


def _deltas(rows, key):
    """Consecutive changes, skipping gaps. Returns [(ts, delta)] where ts is
    the LATER sample — the delta is only knowable once that sample lands."""
    out, prev = [], None
    for r in rows:
        v = r.get(key)
        if v is None:
            prev = None       # a gap breaks the chain; don't bridge across it
            continue
        if prev is not None:
            out.append((r["ts"], v - prev))
        prev = v
    return out


def score_metric(rows, key, pre_start, pre_end):
    """How unusual was this metric's rate of change during the run-up?

    z of the run-up's MEAN delta against the same session's other deltas. The
    baseline is the rest of the day, so a metric that drifts all session long
    doesn't score just for drifting."""
    ds = _deltas(rows, key)
    if len(ds) < MIN_SAMPLES:
        return {"metric": key, "verdict": "insufficient", "n": len(ds)}
    pre = [d for ts, d in ds if pre_start <= ts <= pre_end]
    rest = [d for ts, d in ds if not (pre_start <= ts <= pre_end)]
    if len(pre) < MIN_PRE_SAMPLES or len(rest) < 2:
        return {"metric": key, "verdict": "insufficient",
                "n": len(ds), "n_pre": len(pre)}
    mu, sd = _mean_sd(rest)
    pre_mu = sum(pre) / len(pre)
    if not sd:
        return {"metric": key, "verdict": "flat-baseline", "n_pre": len(pre)}
    z = (pre_mu - mu) / sd
    return {"metric": key, "verdict": "ok", "z": z, "pre_mean": pre_mu,
            "base_mean": mu, "base_sd": sd, "n_pre": len(pre), "n": len(ds),
            "notable": abs(z) >= Z_NOTABLE}


def clues(store, underlying: str, day, move, lookback_min: int = 45,
          sample: int = 5, max_age_min: int = 10, timeline=None):
    """Everything that looked abnormal in the `lookback_min` before `move`."""
    tl = timeline or session_timeline(store, underlying, day, sample=sample,
                                      max_age_min=max_age_min)
    pre_end = move["start"]
    pre_start = pre_end - timedelta(minutes=lookback_min)

    scored = [score_metric(tl["rows"], m, pre_start, pre_end) for m in METRICS]
    ok = [s for s in scored if s["verdict"] == "ok"]
    ok.sort(key=lambda s: abs(s["z"]), reverse=True)

    ib_before = store.index_bias_asof(underlying, pre_start, max_age_min=30)
    ib_at = store.index_bias_asof(underlying, pre_end, max_age_min=30)
    return {
        "move": move, "expiry": tl["expiry"], "dropped": tl["dropped"],
        "pre_start": pre_start, "pre_end": pre_end,
        "scored": ok, "skipped": [s for s in scored if s["verdict"] != "ok"],
        "index_bias_before": ib_before, "index_bias_at": ib_at,
    }


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

_LABEL = {
    "pcr_oi": "PCR(oi)", "atm_iv": "ATM IV", "iv_skew": "IV skew",
    "call_oi": "call OI", "put_oi": "put OI", "max_pain": "max pain",
}


def _fmt_sig(v):
    if v is None:
        return "—"
    a = abs(v)
    return f"{v:,.0f}" if a >= 1000 else (f"{v:.3f}" if a < 1 else f"{v:.2f}")


def build_report(store, underlying: str, day, window_min: int = 30,
                 lookback_min: int = 45, sample: int = 5, top_n: int = 3,
                 max_age_min: int = 10, interval_min: int = 5) -> str:
    """Formatted 'was there a tell?' report for one underlying x day."""
    base = datetime.combine(day, datetime.min.time())
    bars = store.underlying_bars(underlying, base.replace(hour=9, minute=0),
                                 base.replace(hour=15, minute=35), interval_min)
    L = [f"\n{'=' * 74}", f"{underlying}  {day}  — pre-move clue sweep", "=" * 74]
    if not bars:
        L.append("no underlying_bars recorded for this day — nothing to scan")
        return "\n".join(L)

    day_lo = min(b.low for b in bars)
    day_hi = max(b.high for b in bars)
    L.append(f"Session: {bars[0].open:.1f} -> {bars[-1].close:.1f}  "
             f"(range {day_lo:.1f}-{day_hi:.1f}, {len(bars)} bars)")

    moves = find_moves(bars, window_min, interval_min, top_n)
    if not moves:
        L.append("no move large enough to scan")
        return "\n".join(L)

    tl = session_timeline(store, underlying, day, sample=sample,
                          max_age_min=max_age_min)
    have = sum(1 for r in tl["rows"] if r["pcr_oi"] is not None)
    L.append(f"Chain timeline: {have}/{len(tl['rows'])} samples usable"
             + (f", expiry {tl['expiry'][0]}+{tl['expiry'][1]}" if tl["expiry"] else "")
             + (f", {tl['dropped']} off-expiry dropped" if tl["dropped"] else ""))
    if not have:
        L.append("  (no chain data this day — spot-behaviour clues only)")

    sp = spot_stats(bars)
    for n, mv in enumerate(moves, 1):
        c = clues(store, underlying, day, mv, lookback_min, sample,
                  max_age_min, timeline=tl)
        L.append(f"\n{'-' * 74}")
        L.append(f"MOVE #{n}: {mv['start']:%H:%M} -> {mv['end']:%H:%M}   "
                 f"{mv['from']:.1f} -> {mv['to']:.1f}   "
                 f"{mv['move']:+.1f} ({mv['pct']:+.2f}%)")
        L.append(f"Run-up scanned: {c['pre_start']:%H:%M} -> {c['pre_end']:%H:%M} "
                 f"({lookback_min}m before it started)")

        pre = [s for s in sp if c["pre_start"] <= s["ts"] <= c["pre_end"]]
        rest = [s for s in sp if not (c["pre_start"] <= s["ts"] <= c["pre_end"])]
        if len(pre) >= MIN_PRE_SAMPLES and len(rest) >= 2:
            for lbl, k in (("bar range", "range"), ("bar |move|", "ret_abs"),
                           ("volume", "volume")):
                mu, sd = _mean_sd([s[k] for s in rest])
                pmu = sum(s[k] for s in pre) / len(pre)
                if sd:
                    z = (pmu - mu) / sd
                    tag = "  <== NOTABLE" if abs(z) >= Z_NOTABLE else ""
                    L.append(f"  spot {lbl:<11} run-up {_fmt_sig(pmu):>12}  "
                             f"vs day {_fmt_sig(mu):>12}   z={z:+.2f}{tag}")

        if c["scored"]:
            L.append("  chain metric   run-up mean Δ   day mean Δ      z")
            for s in c["scored"]:
                tag = "  <== NOTABLE" if s.get("notable") else ""
                L.append(f"  {_LABEL[s['metric']]:<13} {_fmt_sig(s['pre_mean']):>13} "
                         f"{_fmt_sig(s['base_mean']):>13}  {s['z']:+6.2f}{tag}")
        for why, label in (("insufficient", "too few samples"),
                           ("flat-baseline", "no movement all day to compare against")):
            names = [s["metric"] for s in c["skipped"] if s["verdict"] == why]
            if names:
                L.append(f"  (not scored — {label}: {', '.join(names)})")

        ibb, iba = c["index_bias_before"], c["index_bias_at"]
        if ibb or iba:
            def d(x):
                return f"{x['label']} {x['score']:+.2f}" if x else "no reading"
            L.append(f"  index bias   {d(ibb)}  ->  {d(iba)}")

    L.append(f"\n{'=' * 74}")
    L.append("A clue here is a HYPOTHESIS from ONE day. Re-run across many "
             "sessions before\ntrusting any of it — one day cannot tell signal "
             "from coincidence.")
    return "\n".join(L)
