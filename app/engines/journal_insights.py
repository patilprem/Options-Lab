"""
Trade-journal analytics for the scanner auto-trader
===================================================
Pure functions over the rich journal rows (registry.scanner_journal) that the
trader writes on every entry/exit. `analyze()` turns the closed-trade rows
into aggregate evidence — win rate, expectancy by score band / entry hour /
buildup / exit reason, MFE-giveback on trailing exits, churn re-entries — and
derives SUGGESTIONS: concrete, evidence-backed parameter or behaviour changes.

Honesty rules baked in:
- no suggestion fires below a minimum sample (per-bucket AND overall) — a
  3-trade "pattern" is noise, not evidence;
- every suggestion carries the numbers that justify it, so it can be checked;
- the module never mutates settings — it proposes, the human disposes.

All percentages are of the ENTRY PREMIUM (option % move), not of capital:
mfe_pct  = how far the premium ran above entry at its best (max favourable),
mae_pct  = how far it fell below entry at its worst (max adverse),
ret_pct  = the realized premium move at exit.
"""

from __future__ import annotations

MIN_TRADES = 8          # below this, only "keep collecting" is honest
MIN_BUCKET = 5          # min closed trades in a bucket before it's evidence
CHURN_MINUTES = 30      # re-entry within this of the previous exit = churn


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


def _bucket_stats(rows: list[dict]) -> dict:
    pnls = [t.get("realized") for t in rows if t.get("realized") is not None]
    wins = [p for p in pnls if p > 0]
    return {
        "n": len(rows),
        "total": _r(sum(pnls)) if pnls else 0.0,
        "avg": _r(_avg(pnls)),
        "win_rate": _r(len(wins) / len(pnls), 3) if pnls else None,
        "avg_mfe_pct": _r(_avg([t.get("mfe_pct") for t in rows])),
        "avg_mae_pct": _r(_avg([t.get("mae_pct") for t in rows])),
    }


def _entry_hour(t: dict) -> int | None:
    ts = t.get("entry_ts") or ""
    try:
        return int(ts[11:13])
    except (ValueError, IndexError):
        return None


def _score_band(score) -> str:
    if score is None:
        return "unknown"
    if score < 65:
        return "<65"
    if score < 75:
        return "65-75"
    return "75+"


def _hour_band(h) -> str:
    if h is None:
        return "unknown"
    if h < 11:
        return "09-11"
    if h < 13:
        return "11-13"
    return "13+"


def find_churn(exits: list[dict], window_min: int = CHURN_MINUTES) -> list[dict]:
    """Round trips whose ENTRY came within `window_min` minutes of the SAME
    symbol's previous exit — the stopped-out-then-rebought pattern. Works on
    exit rows alone because each carries both its entry_ts and exit ts."""
    from datetime import datetime

    def _dt(s):
        try:
            return datetime.fromisoformat((s or "").replace("Z", ""))
        except (ValueError, TypeError):
            return None

    by_symbol: dict[str, list[dict]] = {}
    for t in exits:
        if t.get("symbol"):
            by_symbol.setdefault(t["symbol"], []).append(t)
    churned = []
    for trades in by_symbol.values():
        trades.sort(key=lambda t: t.get("entry_ts") or "")
        for prev, cur in zip(trades, trades[1:]):
            prev_exit, cur_entry = _dt(prev.get("ts")), _dt(cur.get("entry_ts"))
            if prev_exit and cur_entry and \
                    0 <= (cur_entry - prev_exit).total_seconds() <= window_min * 60:
                churned.append(cur)
    return churned


def suggestions_from(stats: dict, exits: list[dict]) -> list[dict]:
    """Rule-based suggestions, each with its evidence. Only fires on
    sufficient samples; returns [] when the data doesn't support a change."""
    out: list[dict] = []

    # 1) trailing exits giving back too much of the run
    tr = [t for t in exits if t.get("reason") == "trail_stop"]
    if len(tr) >= MIN_BUCKET:
        givebacks = [t["mfe_pct"] - t["ret_pct"] for t in tr
                     if t.get("mfe_pct") is not None and t.get("ret_pct") is not None]
        med_gb, med_mfe = _median(givebacks), _median([t.get("mfe_pct") for t in tr])
        if med_gb is not None and med_mfe and med_gb > 0.5 * med_mfe:
            out.append({
                "rule": "trail_giveback",
                "suggestion": "Trailing exits give back more than half the peak "
                              "run — consider a tighter trail_pct.",
                "evidence": f"{len(tr)} trail exits: median peak +{_r(med_mfe)}% "
                            f"of entry premium, median giveback {_r(med_gb)}%."})

    # 2) hard stops hitting fast = entries are chasing extended moves
    hs = [t for t in exits if t.get("reason") == "hard_stop"]
    if len(hs) >= MIN_BUCKET:
        med_min = _median([t.get("held_minutes") for t in hs])
        if med_min is not None and med_min < 90:
            out.append({
                "rule": "fast_hard_stops",
                "suggestion": "Hard stops hit within ~1.5h of entry — the "
                              "chasing signature. Consider a confirmation "
                              "entry (wait a cycle / a confirming bar) or an "
                              "entry-freshness filter.",
                "evidence": f"{len(hs)} hard-stop exits, median hold "
                            f"{int(med_min)} min."})

    # 3) low score band losing while the high band wins -> raise entry_score
    bands = stats["by_score_band"]
    lo, hi = bands.get("65-75"), bands.get("75+")
    if lo and hi and lo["n"] >= MIN_BUCKET and hi["n"] >= MIN_BUCKET \
            and (lo["avg"] or 0) < 0 < (hi["avg"] or 0):
        out.append({
            "rule": "raise_entry_score",
            "suggestion": "Setups scoring 65-75 lose while 75+ wins — "
                          "consider raising entry_score to ~75.",
            "evidence": f"65-75: avg ₹{lo['avg']} over {lo['n']} trades; "
                        f"75+: avg ₹{hi['avg']} over {hi['n']}."})

    # 4) late-day entries losing
    hours = stats["by_entry_hour"]
    late, early = hours.get("13+"), hours.get("09-11")
    if late and late["n"] >= MIN_BUCKET and (late["avg"] or 0) < 0 \
            and early and (early["avg"] or 0) > 0:
        out.append({
            "rule": "late_entries",
            "suggestion": "Entries after 13:00 lose while morning entries win "
                          "— consider skipping new entries late in the "
                          "session.",
            "evidence": f"13+: avg ₹{late['avg']} over {late['n']}; "
                        f"09-11: avg ₹{early['avg']} over {early['n']}."})

    # 5) stop-and-rebuy churn burning money
    churned = find_churn(exits)
    if len(churned) >= 3:
        churn_pnl = sum(t.get("realized") or 0 for t in churned)
        if churn_pnl < 0:
            out.append({
                "rule": "churn",
                "suggestion": f"Re-entries within {CHURN_MINUTES} min of an "
                              "exit are losing — consider a re-entry cooldown "
                              "per symbol.",
                "evidence": f"{len(churned)} quick re-entries, net "
                            f"₹{_r(churn_pnl)}."})

    # 6) winners never draw down much -> hard stop is wider than needed
    winners = [t for t in exits if (t.get("realized") or 0) > 0]
    cfg_stop = stats.get("config", {}).get("hard_stop_pct")
    if len(winners) >= MIN_BUCKET and cfg_stop:
        worst_win_mae = max((t.get("mae_pct") or 0) for t in winners)
        if worst_win_mae < cfg_stop * 100 / 2:
            out.append({
                "rule": "tighten_hard_stop",
                "suggestion": "No winner ever drew down close to the hard "
                              "stop — a tighter hard_stop_pct would cut the "
                              "loss on failed trades (and lets sizing risk "
                              "the same ₹ on more lots).",
                "evidence": f"worst winner drawdown {_r(worst_win_mae)}% vs "
                            f"hard stop {_r(cfg_stop * 100)}%, "
                            f"{len(winners)} winners."})

    # 7) covering/unwinding-fuelled entries underperforming fresh buildup
    bu = stats["by_buildup"]
    fresh = [bu.get(k) for k in ("long_buildup", "short_buildup") if bu.get(k)]
    stale = [bu.get(k) for k in ("short_covering", "long_unwinding") if bu.get(k)]
    fresh_n = sum(b["n"] for b in fresh)
    stale_n = sum(b["n"] for b in stale)
    if fresh_n >= MIN_BUCKET and stale_n >= MIN_BUCKET:
        fresh_avg = sum((b["avg"] or 0) * b["n"] for b in fresh) / fresh_n
        stale_avg = sum((b["avg"] or 0) * b["n"] for b in stale) / stale_n
        if stale_avg < 0 < fresh_avg:
            out.append({
                "rule": "fresh_buildup_only",
                "suggestion": "Trades fuelled by covering/unwinding lose while "
                              "fresh-buildup trades win — consider entering "
                              "only on long/short buildup.",
                "evidence": f"fresh: avg ₹{_r(fresh_avg)} ({fresh_n}); "
                            f"covering/unwinding: avg ₹{_r(stale_avg)} "
                            f"({stale_n})."})

    # 8) fees eating the edge
    ov = stats["overall"]
    gross_wins = sum(t.get("realized") or 0 for t in winners)
    total_fees = sum((t.get("entry_fees") or 0) + (t.get("exit_fees") or 0)
                     for t in exits)
    if ov["n"] >= MIN_TRADES and gross_wins > 0 and total_fees > 0.3 * gross_wins:
        out.append({
            "rule": "fee_drag",
            "suggestion": "Fees are a large share of what winners make — "
                          "fewer, higher-conviction trades (higher "
                          "entry_score, longer holds) would keep more of it.",
            "evidence": f"₹{_r(total_fees)} total fees vs ₹{_r(gross_wins)} "
                        f"from winners."})
    return out


def analyze(exits: list[dict], config: dict | None = None) -> dict:
    """Aggregate a list of closed-trade journal rows (kind='exit', each row a
    self-contained round trip) into stats + suggestions. `config` is the
    CURRENT trader config (hard_stop_pct etc), used to compare observed
    behaviour against configured levels."""
    exits = [t for t in exits if t.get("kind", "exit") == "exit"]
    pnls = [t.get("realized") for t in exits if t.get("realized") is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    by_reason: dict[str, list] = {}
    by_band: dict[str, list] = {}
    by_hour: dict[str, list] = {}
    by_buildup: dict[str, list] = {}
    for t in exits:
        by_reason.setdefault(t.get("reason") or "unknown", []).append(t)
        by_band.setdefault(_score_band(t.get("entry_score")), []).append(t)
        by_hour.setdefault(_hour_band(_entry_hour(t)), []).append(t)
        bu = (t.get("entry_ctx") or {}).get("buildup") or "unknown"
        by_buildup.setdefault(bu, []).append(t)

    profit_factor = None
    if losses and sum(losses) != 0:
        profit_factor = _r(sum(wins) / abs(sum(losses)))

    stats = {
        "overall": {
            "n": len(exits),
            "wins": len(wins), "losses": len(losses),
            "win_rate": _r(len(wins) / len(pnls), 3) if pnls else None,
            "total": _r(sum(pnls)) if pnls else 0.0,
            "expectancy": _r(_avg(pnls)),
            "avg_win": _r(_avg(wins)), "avg_loss": _r(_avg(losses)),
            "profit_factor": profit_factor,
            "median_held_minutes": _r(_median(
                [t.get("held_minutes") for t in exits]), 0),
            "total_fees": _r(sum((t.get("entry_fees") or 0) +
                                 (t.get("exit_fees") or 0) for t in exits)),
        },
        "by_reason": {k: _bucket_stats(v) for k, v in by_reason.items()},
        "by_score_band": {k: _bucket_stats(v) for k, v in by_band.items()},
        "by_entry_hour": {k: _bucket_stats(v) for k, v in by_hour.items()},
        "by_buildup": {k: _bucket_stats(v) for k, v in by_buildup.items()},
        "config": config or {},
    }
    if len(exits) < MIN_TRADES:
        stats["ready"] = False
        stats["suggestions"] = [{
            "rule": "insufficient_data",
            "suggestion": "Not enough closed trades to draw conclusions — "
                          "keep the paper book running unchanged.",
            "evidence": f"{len(exits)} closed trades; need {MIN_TRADES}."}]
    else:
        stats["ready"] = True
        stats["suggestions"] = suggestions_from(stats, exits)
    return stats
