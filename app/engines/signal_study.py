"""Does a signal actually predict anything? — historical validation harness.

Built because a strategy was proposed off ONE observed day (2026-08-17: iv_skew
falling while index_bias stayed bearish, followed by a rally). One day cannot
distinguish a signal from a coincidence, and a strategy tuned on one day is
just that day written in Python.

So this measures the CONDITION, not a strategy: over every recorded session,
find every bar where the condition fired, measure what price did next, and
compare that against what price did on ALL bars. That last part is the whole
point — a rule that is right 55% of the time is worth nothing if the market
rose 55% of the time anyway. The baseline is the null hypothesis, and it is
computed from the same bars, same days, same underlying.

Three things here exist specifically to make overfitting visible:

  * BASELINE COMPARISON. Every conditional number is reported next to the
    unconditional one. `edge` is the difference, and it is the only number
    worth looking at.
  * THRESHOLD SWEEP. The same study is run across a grid of thresholds. A real
    effect degrades gracefully as you move off the best setting — it shows up
    as a PLATEAU. Noise shows up as one lucky cell surrounded by nothing, and
    a sweep makes that obvious in a way a single backtest number never does.
  * SAMPLE GATING. Below MIN_FIRINGS the verdict is 'insufficient', never a
    hit rate. A 70% win rate off 6 trades is not evidence.

Direction is reported separately for bullish and bearish firings: a rule that
works one way only is a real and common finding, and averaging the two hides it.

PURE and READ-ONLY.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from app.engines.premove import session_timeline

MIN_FIRINGS = 20          # below this, no verdict is issued
DEFAULT_HORIZONS = (15, 30, 60)


# --------------------------------------------------------------------------
# Per-day series
# --------------------------------------------------------------------------

def day_series(store, underlying: str, day, sample: int = 5,
               max_age_min: int = 10):
    """Aligned [{ts, skew, bias, spot}] for one session, or [] if unusable."""
    base = datetime.combine(day, datetime.min.time())
    bars = store.underlying_bars(underlying, base.replace(hour=9, minute=0),
                                 base.replace(hour=15, minute=35), sample)
    if not bars:
        return []
    spot_at = {b.ts: b.close for b in bars}

    tl = session_timeline(store, underlying, day, sample=sample,
                          max_age_min=max_age_min)
    out = []
    for r in tl["rows"]:
        ts = r["ts"]
        if ts not in spot_at:
            continue
        ib = store.index_bias_asof(underlying, ts, max_age_min=30)
        out.append({"ts": ts, "skew": r["iv_skew"], "spot": spot_at[ts],
                    "bias": (ib or {}).get("score")})
    return out


def forward_returns(series, i: int, horizons, sample: int = 5):
    """Point move from bar i to i+h minutes ahead. None when the day ends
    first — a truncated horizon must not be silently treated as a flat one."""
    out = {}
    for h in horizons:
        j = i + h // sample
        out[h] = (series[j]["spot"] - series[i]["spot"]) if j < len(series) else None
    return out


# --------------------------------------------------------------------------
# Firing detection
# --------------------------------------------------------------------------

def firings(series, lookback_bars: int, min_shift: float, bias_gate: float,
            horizons=DEFAULT_HORIZONS, sample: int = 5):
    """Every bar where the divergence condition held, with forward returns.

    A gap in skew or bias breaks the comparison rather than bridging it: a
    poller outage must never manufacture an enormous fake 'shift'.
    """
    out = []
    for i in range(lookback_bars, len(series)):
        cur, prev = series[i], series[i - lookback_bars]
        if cur["skew"] is None or prev["skew"] is None or cur["bias"] is None:
            continue
        # any gap inside the lookback invalidates the shift
        if any(series[k]["skew"] is None for k in range(i - lookback_bars, i + 1)):
            continue
        shift = cur["skew"] - prev["skew"]
        side = None
        if shift <= -min_shift and cur["bias"] <= -bias_gate:
            side = "bullish"
        elif shift >= min_shift and cur["bias"] >= bias_gate:
            side = "bearish"
        if side:
            out.append({"ts": cur["ts"], "side": side, "shift": shift,
                        "bias": cur["bias"], "spot": cur["spot"],
                        "fwd": forward_returns(series, i, horizons, sample)})
    return out


def baseline(series, horizons=DEFAULT_HORIZONS, sample: int = 5):
    """Unconditional forward returns over every bar — the null hypothesis."""
    out = {h: [] for h in horizons}
    for i in range(len(series)):
        for h, v in forward_returns(series, i, horizons, sample).items():
            if v is not None:
                out[h].append(v)
    return out


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def _stats(values):
    n = len(values)
    if not n:
        return {"n": 0, "mean": None, "hit": None, "se": None}
    mean = sum(values) / n
    hit = sum(1 for v in values if v > 0) / n
    if n > 1:
        var = sum((v - mean) ** 2 for v in values) / (n - 1)
        se = (var / n) ** 0.5
    else:
        se = None
    return {"n": n, "mean": mean, "hit": hit, "se": se}


def summarize(all_firings, base, horizons=DEFAULT_HORIZONS):
    """Conditional vs unconditional, per side and horizon.

    `edge` is conditional mean minus baseline mean, SIGNED BY SIDE so a
    bearish firing that correctly precedes a fall counts as positive edge.
    `z` is that edge in standard errors — a rough scale, not a p-value.
    """
    out = {}
    for side in ("bullish", "bearish"):
        sgn = 1.0 if side == "bullish" else -1.0
        rows = {}
        for h in horizons:
            vals = [f["fwd"][h] for f in all_firings
                    if f["side"] == side and f["fwd"].get(h) is not None]
            cond = _stats([v * sgn for v in vals])
            b = _stats([v * sgn for v in base.get(h, [])])
            if cond["n"] < MIN_FIRINGS:
                rows[h] = {"verdict": "insufficient", **cond,
                           "base_mean": b["mean"], "base_hit": b["hit"]}
                continue
            edge = cond["mean"] - (b["mean"] or 0.0)
            rows[h] = {
                "verdict": "ok", **cond,
                "base_mean": b["mean"], "base_hit": b["hit"], "base_n": b["n"],
                "edge": edge,
                "hit_edge": (cond["hit"] - b["hit"]) if b["hit"] is not None else None,
                "z": (edge / cond["se"]) if cond["se"] else None,
            }
        out[side] = rows
    return out


# --------------------------------------------------------------------------
# Multi-day study + threshold sweep
# --------------------------------------------------------------------------

def load_days(store, underlying: str, start, end, sample: int = 5,
              max_age_min: int = 10, progress=None):
    """Series for every weekday in the range that has usable data.

    Loaded ONCE and reused across the whole threshold sweep — re-reading the
    store per grid cell would make the sweep unusably slow on the VPS.
    """
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            s = day_series(store, underlying, d, sample, max_age_min)
            if any(r["skew"] is not None for r in s):
                days.append((d, s))
            if progress:
                progress(d, len(days))
        d += timedelta(days=1)
    return days


def study(days, lookback_bars: int, min_shift: float, bias_gate: float,
          horizons=DEFAULT_HORIZONS, sample: int = 5):
    """Run the condition across pre-loaded days and summarize."""
    all_f, base = [], {h: [] for h in horizons}
    for _, s in days:
        all_f += firings(s, lookback_bars, min_shift, bias_gate, horizons, sample)
        for h, vals in baseline(s, horizons, sample).items():
            base[h] += vals
    return {"firings": all_f, "summary": summarize(all_f, base, horizons),
            "n_days": len(days), "baseline": base}


def sweep(days, lookbacks, shifts, gates, horizon: int, sample: int = 5):
    """The overfit detector.

    A real effect degrades gracefully off its best setting — neighbouring
    cells stay positive, and the surface reads as a PLATEAU. Noise produces
    one bright cell surrounded by nothing. Reading a single tuned number can
    never tell those apart; this can.
    """
    out = []
    for lb in lookbacks:
        for sh in shifts:
            for g in gates:
                r = study(days, lb, sh, g, (horizon,), sample)
                for side, rows in r["summary"].items():
                    row = rows[horizon]
                    out.append({"lookback": lb, "shift": sh, "gate": g,
                                "side": side, "n": row["n"],
                                "verdict": row["verdict"],
                                "edge": row.get("edge"),
                                "hit": row.get("hit"),
                                "z": row.get("z")})
    return out


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def _f(v, nd=2):
    return "—" if v is None else f"{v:+.{nd}f}"


def build_report(store, underlying: str, start, end, lookback_bars: int = 9,
                 min_shift: float = 0.5, bias_gate: float = 0.3,
                 horizons=DEFAULT_HORIZONS, sample: int = 5,
                 max_age_min: int = 10, do_sweep: bool = True) -> str:
    L = [f"\n{'=' * 78}",
         f"{underlying}  skew/bias divergence — signal validation  {start} .. {end}",
         "=" * 78]
    days = load_days(store, underlying, start, end, sample, max_age_min)
    if not days:
        L.append("no days with recorded chain data in this range — nothing to test")
        return "\n".join(L)
    L.append(f"Days with usable chain data: {len(days)}")

    r = study(days, lookback_bars, min_shift, bias_gate, horizons, sample)
    L.append(f"Settings: lookback={lookback_bars} bars, min_shift={min_shift}, "
             f"bias_gate={bias_gate}")
    L.append(f"Total firings: {len(r['firings'])}")

    for side, rows in r["summary"].items():
        L.append(f"\n  {side.upper()} firings")
        L.append(f"  {'horizon':>8} {'n':>5} {'mean':>9} {'base':>9} "
                 f"{'EDGE':>9} {'hit':>7} {'base':>7} {'z':>6}")
        for h in horizons:
            x = rows[h]
            if x["verdict"] != "ok":
                L.append(f"  {h:>6}m {x['n']:>5}   insufficient sample "
                         f"(need {MIN_FIRINGS}) — no verdict")
                continue
            L.append(f"  {h:>6}m {x['n']:>5} {_f(x['mean']):>9} "
                     f"{_f(x['base_mean']):>9} {_f(x['edge']):>9} "
                     f"{x['hit'] * 100:>6.1f}% {x['base_hit'] * 100:>6.1f}% "
                     f"{_f(x['z'], 1):>6}")

    if do_sweep:
        h = horizons[len(horizons) // 2]
        L.append(f"\n  THRESHOLD SWEEP @ {h}m — looking for a PLATEAU, not a "
                 f"lucky cell")
        L.append(f"  {'lb':>3} {'shift':>6} {'gate':>5} {'side':>8} {'n':>5} "
                 f"{'EDGE':>9} {'z':>6}")
        rows = sweep(days, [max(3, lookback_bars - 3), lookback_bars,
                            lookback_bars + 3],
                     [round(min_shift * m, 2) for m in (0.6, 0.8, 1.0, 1.3)],
                     [round(bias_gate * m, 2) for m in (0.7, 1.0, 1.4)],
                     h, sample)
        for x in rows:
            mark = "" if x["verdict"] == "ok" else "  (small n)"
            L.append(f"  {x['lookback']:>3} {x['shift']:>6} {x['gate']:>5} "
                     f"{x['side']:>8} {x['n']:>5} {_f(x['edge']):>9} "
                     f"{_f(x['z'], 1):>6}{mark}")
        ok = [x for x in rows if x["verdict"] == "ok"]
        pos = [x for x in ok if (x["edge"] or 0) > 0]
        L.append(f"\n  {len(ok)}/{len(rows)} cells had a usable sample; "
                 f"{len(pos)} of those showed positive edge.")
        if ok and len(pos) <= max(1, len(ok) // 5):
            L.append("  -> Edge appears in only a FEW cells. That is what noise "
                     "looks like.\n     Treat this signal as unproven.")

    L.append(f"\n{'=' * 78}")
    L.append("EDGE is the only column that matters: conditional mean minus the\n"
             "unconditional mean over the same bars. A high hit rate with zero\n"
             "edge means the market simply moved that way anyway.")
    return "\n".join(L)
