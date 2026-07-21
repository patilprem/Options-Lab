"""Multi-index pivot-confluence report.

Checks whether NIFTY *and* BANKNIFTY are simultaneously bouncing near one of
their own prior-session pivot levels — the "both indices reacting off support
together" pattern that's a stronger tell than either alone. Pure, store-only
reads (like app/engines/event_signals.py), so this works both offline (CLI,
app process not running) and live (via GET /data/pivot_confluence, reusing
the app's already-open store connection).

For each underlying, at a given as-of timestamp:
  1. Classic floor pivots (P/S1/S2/S3/R1/R2/R3) + CPR (bc/tc) from the PRIOR
     session's H/L/C (indicators.pivots_from_history / indicators.cpr).
  2. Is the current spot within `tolerance_pct` of any of those levels?
  3. Reversal confirmation: range_position of the latest bar (close near the
     bar's high = rejected a low, near the low = rejected a high),
     is_outside_bar, break_of_structure, RSI — all from indicators.py.
  4. Chain read (PCR/IV-skew/max-pain) + index_bias at that timestamp, same
     as event_signals.py, as a same-direction filter.

The report is descriptive, not a signal generator — it surfaces the numbers;
a human (or a future backtested Strategy) still decides what they mean. This
is why it stays a report, not a Strategy: the codebase's own discipline is
"validate on history before this drives real entries" (walkforward.py /
strategy_adapt.py), not "one visually-appealing pattern -> auto-trade".
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.engines import indicators
from app.engines.event_signals import _cache_at, _fmt, _describe_bias
from app.engines import scanner

DEFAULT_TOLERANCE_PCT = 0.15
DEFAULT_LOOKBACK_DAYS = 5


def _levels(prev_ohlc: dict) -> dict:
    piv = indicators.pivots(prev_ohlc["high"], prev_ohlc["low"], prev_ohlc["close"])
    c = indicators.cpr(prev_ohlc["high"], prev_ohlc["low"], prev_ohlc["close"])
    return {"R3": piv["r3"], "R2": piv["r2"], "R1": piv["r1"], "P": piv["p"],
            "CPR-TC": c["tc"], "CPR-BC": c["bc"],
            "S1": piv["s1"], "S2": piv["s2"], "S3": piv["s3"]}


def _nearby_levels(spot: float, levels: dict, tolerance_pct: float) -> list:
    """Levels within tolerance_pct of spot, nearest first: (name, level, delta)."""
    tol = spot * tolerance_pct / 100.0
    hits = [(name, lvl, spot - lvl) for name, lvl in levels.items() if abs(spot - lvl) <= tol]
    hits.sort(key=lambda h: abs(h[2]))
    return hits


def analyze_underlying(store, underlying: str, asof: datetime,
                       tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
                       lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                       max_age_min: int = 10) -> dict:
    """One underlying's pivot-confluence read as-of `asof`. Always returns a
    dict; check `error` for why a field might be missing (no history yet,
    single-session data, no chain recorded near `asof`)."""
    bars = store.underlying_bars(underlying, asof - timedelta(days=lookback_days),
                                 asof, interval_min=5)
    bars = [b for b in bars if b.ts <= asof]
    if not bars:
        return {"underlying": underlying, "asof": asof, "error": "no underlying_bars recorded"}

    prev = indicators.prev_day(bars)
    if prev is None:
        return {"underlying": underlying, "asof": asof,
                "error": "only one session in history — need multi-day warmup for pivots"}

    levels = _levels(prev)
    spot = bars[-1].close
    near = _nearby_levels(spot, levels, tolerance_pct)

    cache = _cache_at(store, underlying, asof, max_age_min)
    chain = None
    if cache:
        chain = scanner.chain_metrics(cache)
        chain["max_pain"] = scanner.max_pain(cache)
    ib = store.index_bias_asof(underlying, asof, max_age_min=max_age_min)

    return {
        "underlying": underlying, "asof": asof, "spot": spot,
        "prev_day": prev, "levels": levels, "near_levels": near,
        "range_position": indicators.range_position(bars[-1]),
        "outside_bar": indicators.is_outside_bar(bars),
        "break_of_structure": indicators.break_of_structure(bars),
        "rsi": indicators.rsi(bars),
        "chain": chain, "index_bias": ib,
    }


def _fmt_level_hits(near: list) -> str:
    if not near:
        return "none"
    return ", ".join(f"{name}={lvl:.1f} ({d:+.1f})" for name, lvl, d in near)


def format_confluence_report(results: list) -> str:
    lines = [f"\n{'=' * 70}",
             f"Pivot confluence @ {results[0]['asof'].strftime('%Y-%m-%d %H:%M') if results else '?'}",
             "=" * 70]
    all_near = True
    for r in results:
        u = r["underlying"]
        if r.get("error"):
            lines.append(f"\n{u}: {r['error']}")
            all_near = False
            continue
        lines.append(f"\n{u}:  spot {r['spot']:.1f}   "
                     f"prev-day H/L/C {r['prev_day']['high']:.1f}/"
                     f"{r['prev_day']['low']:.1f}/{r['prev_day']['close']:.1f}")
        lines.append(f"  levels:  P={r['levels']['P']:.1f}  "
                     f"S1={r['levels']['S1']:.1f}  S2={r['levels']['S2']:.1f}  "
                     f"R1={r['levels']['R1']:.1f}  R2={r['levels']['R2']:.1f}  "
                     f"CPR {r['levels']['CPR-BC']:.1f}-{r['levels']['CPR-TC']:.1f}")
        lines.append(f"  near:    {_fmt_level_hits(r['near_levels'])}")
        if not r["near_levels"]:
            all_near = False
        lines.append(f"  candle:  range_pos={_fmt(r['range_position'], 2)} "
                     f"(1.0=closed at high/rejected the low, 0.0=closed at low/rejected the high)"
                     f"   outside_bar={r['outside_bar']}   "
                     f"BOS={r['break_of_structure']}   RSI={_fmt(r['rsi'], 1)}")
        if r["chain"]:
            lines.append(f"  chain:   PCR(oi)={_fmt(r['chain']['pcr_oi'], 3)}  "
                         f"ATM IV={_fmt(r['chain']['atm_iv'], 2)}  "
                         f"IV skew={_fmt(r['chain']['iv_skew'], 2)}  "
                         f"max pain={_fmt(r['chain']['max_pain'], 0)}")
        else:
            lines.append("  chain:   no chain_snapshots recorded near this timestamp")
        lines.append(f"  index_bias: {_describe_bias(r['index_bias'])}")

    lines.append(f"\n{'-' * 70}")
    verdict = ("ALL underlyings near a pivot level" if all_near and results
              else "NOT all underlyings near a pivot level right now")
    lines.append(f"Confluence: {verdict}")
    lines.append("(descriptive only — confirm direction from range_position/BOS/chain "
                "before treating this as a signal; not backtested yet)")
    return "\n".join(lines)


def build_confluence_report(store, underlyings: list, asof: datetime,
                            tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
                            lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                            max_age_min: int = 10) -> str:
    results = [analyze_underlying(store, u, asof, tolerance_pct, lookback_days, max_age_min)
              for u in underlyings]
    return format_confluence_report(results)
