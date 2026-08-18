"""What does the data do before a move — across ALL recorded history?

Discovery, not confirmation. The earlier study (signal_study.py) tested ONE
pre-specified condition, which is only as good as the guess behind it. This
asks the open question instead: label every bar by what price did NEXT, then
ask which measurable features separate the bars that preceded a real move from
the ones that preceded nothing.

TWO NUMBERS, AND BOTH ARE REQUIRED:

  * RECALL — of all the big moves, what fraction had this feature beforehand?
    "It happens before every move" is a recall claim.
  * PRECISION — of all the times this feature appeared, what fraction were
    actually followed by a move?

Recall alone is the classic trap. A feature present before every one of 40
moves is worthless if it also fired on 400 quiet windows: you would take 440
trades to catch 40. Precision is what makes it tradeable, and it can only be
computed against the bars where NOTHING happened — which is why the control
group is every other bar, not a curated set.

BASE RATE is printed alongside, because precision must beat it to mean
anything. If 12% of all windows precede a move, a feature with 13% precision
has found nothing.

Move size is expressed in ATR multiples as well as points, because "long
enough for decent risk-reward" is a statement about move size relative to the
stop you would need, not about absolute points.

PURE and READ-ONLY. HEAVY — human-triggered, never in the trading loop.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.engines.premove import session_timeline

MIN_CLASS = 15        # below this many moves, no verdict is issued
FEATURES = ("d_skew", "d_pcr", "d_atm_iv", "d_call_oi_pct", "d_put_oi_pct",
            "bias", "maxpain_dist_pct", "compression")

_LABEL = {
    "d_skew": "skew change", "d_pcr": "PCR change", "d_atm_iv": "ATM IV change",
    "d_call_oi_pct": "call OI %chg", "d_put_oi_pct": "put OI %chg",
    "bias": "index bias", "maxpain_dist_pct": "max pain vs spot %",
    "compression": "range compression",
}


# --------------------------------------------------------------------------
# Series
# --------------------------------------------------------------------------

def rich_series(store, underlying: str, day, sample: int = 5,
                max_age_min: int = 10):
    """Every chain metric + spot + bias, aligned on one session's bars."""
    base = datetime.combine(day, datetime.min.time())
    bars = store.underlying_bars(underlying, base.replace(hour=9, minute=0),
                                 base.replace(hour=15, minute=35), sample)
    if not bars:
        return []
    by_ts = {b.ts: b for b in bars}
    tl = session_timeline(store, underlying, day, sample=sample,
                          max_age_min=max_age_min)
    out = []
    for r in tl["rows"]:
        b = by_ts.get(r["ts"])
        if b is None:
            continue
        ib = store.index_bias_asof(underlying, r["ts"], max_age_min=30)
        out.append({"ts": r["ts"], "spot": b.close, "high": b.high, "low": b.low,
                    "bias": (ib or {}).get("score"), **{k: r[k] for k in
                    ("pcr_oi", "atm_iv", "iv_skew", "call_oi", "put_oi", "max_pain")}})
    return out


def _atr(series, i, n=14):
    """Average true range over the preceding n bars — the scale that turns a
    point move into an R multiple."""
    lo = max(1, i - n)
    trs = []
    for k in range(lo, i + 1):
        prev = series[k - 1]["spot"] if k else series[k]["spot"]
        trs.append(max(series[k]["high"] - series[k]["low"],
                       abs(series[k]["high"] - prev),
                       abs(series[k]["low"] - prev)))
    return (sum(trs) / len(trs)) if trs else None


# --------------------------------------------------------------------------
# Features and labels
# --------------------------------------------------------------------------

def _pct_delta(now, then):
    if now is None or then in (None, 0):
        return None
    return (now - then) / abs(then) * 100.0


def features_at(series, i: int, lookback: int):
    """Feature vector at bar i, or None if the lookback has any gap in it.

    A gap invalidates the whole vector rather than being bridged — a poller
    outage would otherwise manufacture enormous fake deltas that dominate
    every ranking."""
    if i < lookback:
        return None
    cur, prev = series[i], series[i - lookback]
    for k in range(i - lookback, i + 1):
        if series[k]["iv_skew"] is None:
            return None
    if cur["bias"] is None:
        return None

    day_hi = max(s["high"] for s in series[:i + 1])
    day_lo = min(s["low"] for s in series[:i + 1])
    win_hi = max(s["high"] for s in series[i - lookback:i + 1])
    win_lo = min(s["low"] for s in series[i - lookback:i + 1])
    compression = ((win_hi - win_lo) / (day_hi - day_lo)) if day_hi > day_lo else None

    mp = cur["max_pain"]
    return {
        "d_skew": (cur["iv_skew"] - prev["iv_skew"]),
        "d_pcr": ((cur["pcr_oi"] - prev["pcr_oi"])
                  if cur["pcr_oi"] is not None and prev["pcr_oi"] is not None else None),
        "d_atm_iv": ((cur["atm_iv"] - prev["atm_iv"])
                     if cur["atm_iv"] is not None and prev["atm_iv"] is not None else None),
        "d_call_oi_pct": _pct_delta(cur["call_oi"], prev["call_oi"]),
        "d_put_oi_pct": _pct_delta(cur["put_oi"], prev["put_oi"]),
        "bias": cur["bias"],
        "maxpain_dist_pct": (((mp - cur["spot"]) / cur["spot"] * 100.0)
                             if mp else None),
        "compression": compression,
    }


def label_at(series, i: int, window_bars: int, min_atr_mult: float,
             min_pct: float):
    """What price did next: 'up', 'down' or 'quiet'.

    A move must clear BOTH an ATR multiple (so it is large relative to the
    stop it would need — your risk-reward requirement) and a percentage floor
    (so a dead-quiet day cannot promote noise into a 'move' just because ATR
    collapsed). None when the day ends before the horizon does.
    """
    j = i + window_bars
    if j >= len(series):
        return None
    move = series[j]["spot"] - series[i]["spot"]
    atr = _atr(series, i)
    if not atr:
        return None
    pct = abs(move) / series[i]["spot"] * 100.0
    if abs(move) >= min_atr_mult * atr and pct >= min_pct:
        return "up" if move > 0 else "down"
    return "quiet"


def collect(days, lookback: int, window_bars: int, min_atr_mult: float,
            min_pct: float):
    """[(label, features, meta)] over every usable bar of every day."""
    out = []
    for day, s in days:
        for i in range(lookback, len(s)):
            lab = label_at(s, i, window_bars, min_atr_mult, min_pct)
            if lab is None:
                continue
            f = features_at(s, i, lookback)
            if f is None:
                continue
            j = min(i + window_bars, len(s) - 1)
            out.append((lab, f, {"day": day, "ts": s[i]["ts"],
                                 "move": s[j]["spot"] - s[i]["spot"]}))
    return out


# --------------------------------------------------------------------------
# Separation, precision / recall
# --------------------------------------------------------------------------

def _mean_sd(xs):
    n = len(xs)
    if not n:
        return None, None
    mu = sum(xs) / n
    if n < 2:
        return mu, None
    return mu, (sum((x - mu) ** 2 for x in xs) / (n - 1)) ** 0.5


def separation(samples, feature: str, target: str):
    """Cohen's d between the target class and everything else.

    Effect size, not significance: with tens of thousands of bars a trivial
    difference reaches 'significance' while being useless to trade. |d| < 0.2
    is negligible however small the p-value would have been.
    """
    t = [f[feature] for lab, f, _ in samples if lab == target and f[feature] is not None]
    c = [f[feature] for lab, f, _ in samples if lab != target and f[feature] is not None]
    if len(t) < MIN_CLASS or len(c) < MIN_CLASS:
        return {"feature": feature, "verdict": "insufficient",
                "n_target": len(t), "n_other": len(c)}
    mt, st = _mean_sd(t)
    mc, sc = _mean_sd(c)
    pooled = (((st or 0) ** 2 + (sc or 0) ** 2) / 2) ** 0.5
    return {"feature": feature, "verdict": "ok", "n_target": len(t),
            "n_other": len(c), "mean_target": mt, "mean_other": mc,
            "d": ((mt - mc) / pooled) if pooled else None}


def precision_recall(samples, feature: str, target: str, direction: int,
                     quantiles=(0.05, 0.10, 0.20, 0.30)):
    """Best precision/recall trade-off for 'feature beyond a threshold'.

    `direction` -1 tests the LOW tail (e.g. skew falling hard), +1 the HIGH
    tail. Thresholds are taken as quantiles of the feature's own distribution,
    so nothing here is tuned to a number picked by hand.

    base_rate is the target class's share of all samples. Precision must beat
    it; a feature whose precision equals the base rate has told you nothing.
    """
    vals = [(f[feature], lab) for lab, f, _ in samples if f[feature] is not None]
    if len(vals) < MIN_CLASS * 4:
        return {"feature": feature, "verdict": "insufficient", "n": len(vals)}
    total_target = sum(1 for v, lab in vals if lab == target)
    if total_target < MIN_CLASS:
        return {"feature": feature, "verdict": "insufficient",
                "n_target": total_target}
    base_rate = total_target / len(vals)

    ordered = sorted(v for v, _ in vals)
    best = None
    for q in quantiles:
        idx = int(q * len(ordered)) if direction < 0 else int((1 - q) * len(ordered))
        idx = min(max(idx, 0), len(ordered) - 1)
        thr = ordered[idx]
        fired = [(v, lab) for v, lab in vals
                 if (v <= thr if direction < 0 else v >= thr)]
        if not fired:
            continue
        hits = sum(1 for _, lab in fired if lab == target)
        prec = hits / len(fired)
        rec = hits / total_target
        lift = prec / base_rate if base_rate else None
        row = {"q": q, "threshold": thr, "n_fired": len(fired), "hits": hits,
               "precision": prec, "recall": rec, "lift": lift}
        if best is None or (row["lift"] or 0) > (best["lift"] or 0):
            best = row
    return {"feature": feature, "verdict": "ok", "direction": direction,
            "base_rate": base_rate, "n_target": total_target, "best": best}


def analyse(samples, target: str):
    """Rank every feature by how well it separates `target` from the rest."""
    rows = []
    for feat in FEATURES:
        sep = separation(samples, feat, target)
        if sep["verdict"] != "ok" or sep["d"] is None:
            rows.append({"sep": sep, "pr": None})
            continue
        pr = precision_recall(samples, feat, target, -1 if sep["d"] < 0 else 1)
        rows.append({"sep": sep, "pr": pr})
    rows.sort(key=lambda r: abs(r["sep"].get("d") or 0), reverse=True)
    return rows


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def _f(v, nd=2):
    return "—" if v is None else f"{v:+.{nd}f}"


def build_report(store, underlying: str, start, end, lookback: int = 9,
                 window_min: int = 30, min_atr_mult: float = 2.0,
                 min_pct: float = 0.35, sample: int = 5,
                 max_age_min: int = 10) -> str:
    from app.engines.signal_study import load_days  # same day loader

    L = [f"\n{'=' * 78}",
         f"{underlying}  move anatomy  {start} .. {end}", "=" * 78]

    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            s = rich_series(store, underlying, d, sample, max_age_min)
            if any(r["iv_skew"] is not None for r in s):
                days.append((d, s))
        d += timedelta(days=1)
    if not days:
        L.append("no days with recorded chain data in this range — nothing to study")
        return "\n".join(L)

    window_bars = max(1, window_min // sample)
    samples = collect(days, lookback, window_bars, min_atr_mult, min_pct)
    counts = {k: sum(1 for lab, _, _ in samples if lab == k)
              for k in ("up", "down", "quiet")}
    L.append(f"Days with chain data: {len(days)}   usable windows: {len(samples)}")
    L.append(f"Move definition: >= {min_atr_mult}x ATR AND >= {min_pct}% "
             f"over {window_min}m   (run-up measured over {lookback * sample}m)")
    L.append(f"Moves found:  UP {counts['up']}   DOWN {counts['down']}   "
             f"QUIET {counts['quiet']}")

    if not samples:
        L.append("\nno usable windows — chain gaps or too few bars")
        return "\n".join(L)

    for target in ("up", "down"):
        L.append(f"\n{'-' * 78}\nBEFORE {target.upper()} MOVES "
                 f"(n={counts[target]})")
        if counts[target] < MIN_CLASS:
            L.append(f"  only {counts[target]} such moves — below the minimum "
                     f"of {MIN_CLASS}. No verdict.\n  Widen the date range or "
                     f"loosen the move definition.")
            continue
        L.append(f"  {'feature':<20} {'before':>9} {'other':>9} {'d':>6} "
                 f"{'prec':>7} {'base':>7} {'lift':>6} {'recall':>7}")
        for row in analyse(samples, target):
            sep, pr = row["sep"], row["pr"]
            if sep["verdict"] != "ok":
                L.append(f"  {_LABEL[sep['feature']]:<20} insufficient sample")
                continue
            line = (f"  {_LABEL[sep['feature']]:<20} {_f(sep['mean_target']):>9} "
                    f"{_f(sep['mean_other']):>9} {_f(sep['d'], 2):>6}")
            if pr and pr.get("verdict") == "ok" and pr.get("best"):
                b = pr["best"]
                line += (f" {b['precision'] * 100:>6.1f}% "
                         f"{pr['base_rate'] * 100:>6.1f}% "
                         f"{b['lift']:>5.2f}x {b['recall'] * 100:>6.1f}%")
            L.append(line)

    L.append(f"\n{'=' * 78}")
    L.append("HOW TO READ THIS\n"
             "  d      — effect size. |d|<0.2 is negligible no matter how many\n"
             "           samples back it; that feature does not separate.\n"
             "  prec   — of the windows where the feature fired, how many were\n"
             "           followed by the move. MUST beat 'base'.\n"
             "  lift   — precision / base rate. 1.0x means the feature added\n"
             "           nothing at all. Below ~1.5x is not worth trading.\n"
             "  recall — of all such moves, how many the feature caught.\n"
             "High recall with 1.0x lift is the classic trap: it fires before\n"
             "every move AND before everything else.")
    return "\n".join(L)
