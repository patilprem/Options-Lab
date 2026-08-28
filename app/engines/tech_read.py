"""
Per-stock technical read for the scanner auto-trader
====================================================
The "experienced trader" half of the entry decision: where price sits vs
session VWAP, whether an intraday trend is established (EMA9/EMA21), whether
a real structural level (opening range / previous-day high-low) has been
BROKEN AND HELD in the trade's direction, how overextended the move is
(RSI / distance from VWAP), and what structural risk:reward a trade here
would carry (next floor-pivot target vs the VWAP/level stop).

Data source: the Tier-1 sweep's `stock_snapshots` rows — the ONLY per-stock
time series recorded (FNO stocks have no underlying_bars). One row per
minute carries the future's LTP and the CUMULATIVE day volume, so we
synthesize close-only pseudo-bars (open=high=low=close=ltp, volume=Δcum).
Consequences, by design:
- EMA / RSI / VWAP are exact enough (VWAP is truly volume-weighted via the
  cumulative-volume deltas);
- ADX / ATR / candle-range constructs are DEGENERATE on flat bars and are
  deliberately NOT computed — a fake trend-strength number is worse than
  none;
- opening-range and break levels are built from minute CLOSES, so ranges run
  slightly narrow; the break test compensates by demanding the last
  `hold_bars` closes ALL beyond the level (break-and-hold, not a touch).

Everything here is PURE (rows in -> dict out); the scanner supplies rows via
store.stock_day_series_bulk / stock_prev_day_levels_bulk (bulk, off-loop)
and attaches the result to ranked_scores() dicts, where
scanner_trader.entry_quality() gates on it. Missing data yields None fields;
the gates fail closed — that is what makes the first ~20 minutes of a
session (EMA/RSI warm-up) and a symbol's first recorded day (no prev-day
pivots) naturally trade-free under the strict profile.

Indicators come from engines/indicators.py only (CLAUDE.md: strategies and
engines must NOT hand-roll these).
"""

from __future__ import annotations

from app.core.contract import Bar
from app.engines import indicators as IND

# NSE session opens 09:15 = minute 555 of the day. The opening-range fields
# are only trustworthy when the recorded series actually STARTS at the open —
# a series beginning mid-day (scanner restart, symbol's first recorded day)
# would otherwise mint a fake "opening" range from whenever recording began.
SESSION_OPEN_MIN = 9 * 60 + 15
OPEN_GRACE_MIN = 5          # first bar must land within this of the open


def snapshot_rows_to_bars(rows: list) -> list[Bar]:
    """(ts, fut_ltp, cumulative_volume) rows, ascending -> close-only Bars
    with per-bar volume = the cumulative delta (clamped at 0: a restarted or
    corrected counter must never mint negative volume). Rows without a
    usable LTP are skipped; their volume still advances the baseline so the
    next bar doesn't inherit a multi-minute lump."""
    bars: list[Bar] = []
    prev_cum = None
    for ts, ltp, cum in rows:
        dv = 0.0
        if cum is not None:
            if prev_cum is not None and cum >= prev_cum:
                dv = float(cum - prev_cum)
            prev_cum = cum
        if ltp is None or ltp <= 0:
            continue
        px = float(ltp)
        bars.append(Bar(ts=ts, open=px, high=px, low=px, close=px, volume=dv))
    return bars


def _r(x, nd=2):
    return None if x is None else round(x, nd)


def _held_break(closes: list, level: float, up: bool, hold_bars: int) -> bool:
    """True when the last `hold_bars` closes are ALL beyond `level` — the
    'break and hold' confirmation, not a single poke through."""
    if len(closes) < hold_bars:
        return False
    tail = closes[-hold_bars:]
    return all(c > level for c in tail) if up else all(c < level for c in tail)


def technical_read(bars_today: list[Bar], prev_levels: dict | None,
                   or_minutes: int = 15, ema_fast_n: int = 9,
                   ema_slow_n: int = 21, rsi_n: int = 14,
                   hold_bars: int = 3) -> dict:
    """One symbol's technical picture from today's pseudo-bars + yesterday's
    levels ({"high","low","close"} or None). Every field is None when the
    data can't support it — never a guess; the entry gates fail closed."""
    out = {
        "n_bars": len(bars_today), "ltp": None,
        "vwap": None, "vwap_dist_pct": None,
        "ema_fast": None, "ema_slow": None, "trend": None,
        "rsi": None,
        "or_high": None, "or_low": None,
        "prev_high": None, "prev_low": None, "prev_close": None,
        "pivots": None,
        "structure_break": None, "break_level": None,
        "target_ce": None, "stop_ce": None, "rr_ce": None,
        "target_pe": None, "stop_pe": None, "rr_pe": None,
    }
    if not bars_today:
        return out
    ltp = bars_today[-1].close
    out["ltp"] = _r(ltp)

    vw = IND.vwap(bars_today)
    if vw:
        out["vwap"] = _r(vw)
        out["vwap_dist_pct"] = _r((ltp - vw) / vw * 100.0)

    ef, es = IND.ema(bars_today, ema_fast_n), IND.ema(bars_today, ema_slow_n)
    out["ema_fast"], out["ema_slow"] = _r(ef), _r(es)
    if ef is not None and es is not None and ef != es:
        out["trend"] = "up" if ef > es else "down"

    out["rsi"] = _r(IND.rsi(bars_today, rsi_n))

    # opening range: only when the series starts AT the session open (see
    # module docstring) AND the window has fully elapsed — a 5-minute-old
    # session has no opening range yet, just an opening.
    first, last = bars_today[0].ts, bars_today[-1].ts
    first_min = first.hour * 60 + first.minute
    if abs(first_min - SESSION_OPEN_MIN) <= OPEN_GRACE_MIN and \
            (last - first).total_seconds() >= or_minutes * 60:
        orng = IND.opening_range(bars_today, or_minutes)
        if orng:
            out["or_high"], out["or_low"] = _r(orng["high"]), _r(orng["low"])

    piv = None
    if prev_levels:
        ph, pl, pc = (prev_levels.get("high"), prev_levels.get("low"),
                      prev_levels.get("close"))
        out["prev_high"], out["prev_low"], out["prev_close"] = \
            _r(ph), _r(pl), _r(pc)
        if ph is not None and pl is not None and pc is not None and ph >= pl:
            piv = IND.pivots(ph, pl, pc)
            out["pivots"] = {k: _r(v) for k, v in piv.items()}

    # break-and-hold of a structural level, in either direction
    closes = [b.close for b in bars_today]
    up_levels = [x for x in (out["or_high"], out["prev_high"])
                 if x is not None]
    dn_levels = [x for x in (out["or_low"], out["prev_low"])
                 if x is not None]
    broken_up = [lv for lv in up_levels
                 if _held_break(closes, lv, up=True, hold_bars=hold_bars)]
    broken_dn = [lv for lv in dn_levels
                 if _held_break(closes, lv, up=False, hold_bars=hold_bars)]
    if broken_up and not broken_dn:
        out["structure_break"], out["break_level"] = "up", _r(max(broken_up))
    elif broken_dn and not broken_up:
        out["structure_break"], out["break_level"] = "down", _r(min(broken_dn))
    # both broken (a wide gap day) = conflicted structure -> no confirmation

    # structural risk:reward, both sides — the gate indexes by the bias.
    # Reward = distance to the NEXT pivot level in the trade's direction;
    # risk = distance back to the protective level (the tighter of VWAP and
    # the broken structure level on the trade's side of price).
    if piv:
        tgt_ce = next((piv[k] for k in ("r1", "r2", "r3") if piv[k] > ltp),
                      None)
        stops_ce = [x for x in (vw, out["break_level"]
                                if out["structure_break"] == "up" else None)
                    if x is not None and x < ltp]
        stop_ce = max(stops_ce) if stops_ce else None
        out["target_ce"], out["stop_ce"] = _r(tgt_ce), _r(stop_ce)
        if tgt_ce is not None and stop_ce is not None and ltp > stop_ce:
            out["rr_ce"] = _r((tgt_ce - ltp) / (ltp - stop_ce))

        tgt_pe = next((piv[k] for k in ("s1", "s2", "s3") if piv[k] < ltp),
                      None)
        stops_pe = [x for x in (vw, out["break_level"]
                                if out["structure_break"] == "down" else None)
                    if x is not None and x > ltp]
        stop_pe = min(stops_pe) if stops_pe else None
        out["target_pe"], out["stop_pe"] = _r(tgt_pe), _r(stop_pe)
        if tgt_pe is not None and stop_pe is not None and stop_pe > ltp:
            out["rr_pe"] = _r((ltp - tgt_pe) / (stop_pe - ltp))
    return out
