"""
Trade-journal analytics for Strategy trades (paper + backtest)
=============================================================
The counterpart to engines/journal_insights.py, but for the Strategy engine:
pure functions over closed round-trip records (attribution.build_round_trip)
that turn a strategy's own trade history into aggregate evidence and concrete,
sample-gated SUGGESTIONS for improving it.

Fed identically by both engines:
  * backtest — from ctx.closed, in _report(), returned in the run result;
  * paper    — from the durable registry.strategy_journal, via
               GET /strategies/{id}/insights and a once-a-day proactive event.

Semantics: P&L is realized ₹ per round trip (multi-leg structures collapse to
one signed number at the position level already). MFE/MAE are the best/worst
unrealized ₹ the trade offered over its life — the excursion that tells you
whether a target was too far or a stop too tight. Reuses attribution() for the
entry-data-state slices rather than reinventing them.

Honesty rules (same as the scanner journal): nothing fires below a minimum
sample overall AND per bucket; every suggestion carries its numbers; the module
proposes, never mutates settings.
"""

from __future__ import annotations

from app.engines.attribution import attribution

MIN_TRADES = 8          # below this, only "keep collecting" is honest
MIN_BUCKET = 5          # min closed trades in a bucket before it's evidence

# entry-data-state slices to compute (key -> numeric bin edges for bucketing)
_ATTR_BINS = {
    "iv_rank": [0, 30, 50, 70, 100],
    "index_bias": [-1, -0.3, 0.3, 1],
    "pcr_oi": [0, 0.8, 1.2, 3],
}


def _median(xs: list) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    mid = n // 2
    return float(xs[mid]) if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def _avg(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _r(x, nd=2):
    return None if x is None else round(x, nd)


def _rupees(x) -> str:
    return "—" if x is None else f"₹{round(x):,}"


def _bucket_stats(rows: list[dict]) -> dict:
    pnls = [t.get("pnl") for t in rows if t.get("pnl") is not None]
    wins = [p for p in pnls if p > 0]
    return {
        "n": len(rows),
        "total": _r(sum(pnls)) if pnls else 0.0,
        "avg": _r(_avg(pnls)),
        "win_rate": _r(len(wins) / len(pnls), 3) if pnls else None,
        "avg_mfe": _r(_avg([t.get("mfe") for t in rows])),
        "avg_mae": _r(_avg([t.get("mae") for t in rows])),
    }


def _entry_hour(t: dict) -> int | None:
    tod = (t.get("entry_context") or {}).get("tod")
    if isinstance(tod, str) and ":" in tod:
        try:
            return int(tod.split(":")[0])
        except ValueError:
            pass
    ts = t.get("entry_ts") or ""
    try:
        return int(ts[11:13])       # "YYYY-MM-DD HH:MM"
    except (ValueError, IndexError):
        return None


def _hour_band(h) -> str:
    if h is None:
        return "unknown"
    if h < 11:
        return "09-11"
    if h < 13:
        return "11-13"
    return "13+"


def suggestions_from(stats: dict, trades: list[dict]) -> list[dict]:
    """Rule-based, evidence-carrying suggestions. Only fires on sufficient
    samples; [] when the data supports no change."""
    out: list[dict] = []
    reasons = stats["by_reason"]
    ov = stats["overall"]
    winners = [t for t in trades if (t.get("pnl") or 0) > 0]
    gross_loss = -sum(t["pnl"] for t in trades if (t.get("pnl") or 0) <= 0)

    # 1) the book is losing AND stops are where the damage is — actionable
    # (stops being most of a PROFITABLE book's losses is them doing their job,
    # so this only fires when expectancy is actually negative)
    sl = reasons.get("stop_loss")
    if sl and sl["n"] >= MIN_BUCKET and (sl["total"] or 0) < 0 \
            and gross_loss > 0 and (ov["total"] or 0) < 0:
        share = -sl["total"] / gross_loss
        if share >= 0.6:
            out.append({
                "rule": "stops_dominate_losses",
                "suggestion": "The book is net negative and declared "
                              "stop-losses are most of the damage — either the "
                              "stop is too tight for this structure's noise, or "
                              "entries are mistimed. Try a wider stop or a "
                              "confirmation filter.",
                "evidence": f"{sl['n']} stop-loss exits = {_rupees(sl['total'])}, "
                            f"{round(share * 100)}% of all losses; book "
                            f"{_rupees(ov['total'])}."})

    # 2) targets rarely hit while winners peak far above what they bank
    tgt = reasons.get("target")
    n_tgt = tgt["n"] if tgt else 0
    if len(winners) >= MIN_BUCKET and n_tgt <= 0.15 * len(winners):
        gb = [t["mfe"] - t["pnl"] for t in winners
              if t.get("mfe") is not None and t.get("pnl") is not None]
        med_gb, med_mfe = _median(gb), _median([t.get("mfe") for t in winners])
        if med_gb and med_mfe and med_mfe > 0 and med_gb > 0.4 * med_mfe:
            out.append({
                "rule": "target_too_far",
                "suggestion": "Winners routinely peak well above where they "
                              "close, and the target almost never triggers — "
                              "consider a nearer target_pct or trailing the "
                              "winners.",
                "evidence": f"{n_tgt} target exits of {len(winners)} winners; "
                            f"median peak {_rupees(med_mfe)}, median giveback "
                            f"{_rupees(med_gb)}."})

    # 3) time exits specifically leaving profit on the table
    te = [t for t in trades if t.get("exit_reason") == "time_exit"]
    if len(te) >= MIN_BUCKET:
        med_mfe = _median([t.get("mfe") for t in te])
        med_pnl = _median([t.get("pnl") for t in te])
        if med_mfe and med_mfe > 0 and med_pnl is not None \
                and (med_mfe - max(med_pnl, 0)) > 0.5 * med_mfe:
            out.append({
                "rule": "time_exit_leaves_profit",
                "suggestion": "Time-stopped trades had banked far less than "
                              "their peak — a target or trailing stop would "
                              "capture more before the time exit fires.",
                "evidence": f"{len(te)} time exits: median peak "
                            f"{_rupees(med_mfe)} vs median realized "
                            f"{_rupees(med_pnl)}."})

    # 4) winning often but still losing money -> losers dwarf winners
    if ov["n"] >= MIN_TRADES and (ov["win_rate"] or 0) >= 0.55 \
            and (ov["expectancy"] or 0) < 0:
        out.append({
            "rule": "neg_expectancy_high_winrate",
            "suggestion": "Win rate is healthy but expectancy is negative — a "
                          "few big losers outweigh many small wins. Tighten "
                          "the stop or let winners run further (raise target).",
            "evidence": f"win rate {round((ov['win_rate'] or 0) * 100)}%, avg "
                        f"win {_rupees(ov['avg_win'])} vs avg loss "
                        f"{_rupees(ov['avg_loss'])}, expectancy "
                        f"{_rupees(ov['expectancy'])}/trade."})

    # 5) an entry-data-state slice with a strong, sample-backed edge
    for key, label in (("iv_rank", "IV rank"), ("index_bias", "index bias"),
                       ("pcr_oi", "PCR(OI)")):
        buckets = stats["attribution"].get(key, {})
        cand = [(b, v) for b, v in buckets.items()
                if b not in ("overall", "unknown") and v["n"] >= MIN_BUCKET]
        if len(cand) < 2:
            continue
        best = max(cand, key=lambda x: x[1]["avg_pnl"])
        worst = min(cand, key=lambda x: x[1]["avg_pnl"])
        if worst[1]["avg_pnl"] < 0 < best[1]["avg_pnl"]:
            out.append({
                "rule": f"filter_{key}",
                "suggestion": f"{label} splits winners from losers — consider "
                              f"only entering when {label} is in the "
                              f"{best[0]} range.",
                "evidence": f"{label} {best[0]}: avg "
                            f"{_rupees(best[1]['avg_pnl'])} ({best[1]['n']}); "
                            f"{worst[0]}: avg {_rupees(worst[1]['avg_pnl'])} "
                            f"({worst[1]['n']})."})

    # 6) a time-of-day window that bleeds
    hours = stats["by_entry_hour"]
    cand = [(b, v) for b, v in hours.items()
            if b != "unknown" and v["n"] >= MIN_BUCKET]
    if cand and (ov["total"] or 0) > 0:
        worst = min(cand, key=lambda x: x[1]["avg"] or 0)
        if (worst[1]["avg"] or 0) < 0:
            out.append({
                "rule": "avoid_time_window",
                "suggestion": f"Entries in the {worst[0]} window lose on "
                              "average while the book is net positive — "
                              "consider skipping new entries then.",
                "evidence": f"{worst[0]}: avg {_rupees(worst[1]['avg'])} over "
                            f"{worst[1]['n']} trades."})

    # 7) fees eating the edge
    total_fees = sum(t.get("fees") or 0 for t in trades)
    gross_wins = sum(t["pnl"] for t in winners)
    if ov["n"] >= MIN_TRADES and gross_wins > 0 and total_fees > 0.3 * gross_wins:
        out.append({
            "rule": "fee_drag",
            "suggestion": "Fees are a large share of gross winnings — fewer, "
                          "higher-conviction trades (or fewer legs) would keep "
                          "more of the edge.",
            "evidence": f"{_rupees(total_fees)} fees vs {_rupees(gross_wins)} "
                        f"gross from winners."})
    return out


def analyze(trades: list[dict], config: dict | None = None) -> dict:
    """Aggregate closed round-trip records -> stats + suggestions.

    trades: [build_round_trip() dicts]. config: current strategy params, if
    any (kept for parity / display; the rules read from the data)."""
    trades = [t for t in trades if t.get("pnl") is not None]
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    by_reason: dict[str, list] = {}
    by_hour: dict[str, list] = {}
    for t in trades:
        by_reason.setdefault(t.get("exit_reason") or "signal", []).append(t)
        by_hour.setdefault(_hour_band(_entry_hour(t)), []).append(t)

    profit_factor = None
    if losses and sum(losses) != 0:
        profit_factor = _r(sum(wins) / abs(sum(losses)))

    stats = {
        "overall": {
            "n": len(trades),
            "wins": len(wins), "losses": len(losses),
            "win_rate": _r(len(wins) / len(pnls), 3) if pnls else None,
            "total": _r(sum(pnls)) if pnls else 0.0,
            "expectancy": _r(_avg(pnls)),
            "avg_win": _r(_avg(wins)), "avg_loss": _r(_avg(losses)),
            "profit_factor": profit_factor,
            "median_held_minutes": _r(_median(
                [t.get("held_minutes") for t in trades]), 0),
            "total_fees": _r(sum(t.get("fees") or 0 for t in trades)),
        },
        "by_reason": {k: _bucket_stats(v) for k, v in by_reason.items()},
        "by_entry_hour": {k: _bucket_stats(v) for k, v in by_hour.items()},
        "attribution": {k: attribution(trades, k, bins=b)
                        for k, b in _ATTR_BINS.items()},
        "config": config or {},
    }
    if len(trades) < MIN_TRADES:
        stats["ready"] = False
        stats["suggestions"] = [{
            "rule": "insufficient_data",
            "suggestion": "Not enough closed trades to draw conclusions — keep "
                          "trading (paper) or widen the backtest window.",
            "evidence": f"{len(trades)} closed trades; need {MIN_TRADES}."}]
    else:
        stats["ready"] = True
        stats["suggestions"] = suggestions_from(stats, trades)
    return stats
