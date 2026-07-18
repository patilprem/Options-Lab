"""
Signal Attribution (step 6 — closing the research loop)
=======================================================
"Which data inputs actually pay?" You already have exit-attribution (why a
trade closed) and walk-forward; the missing half is ENTRY attribution — a
snapshot of the market/signal state at the moment each trade was opened, so
outcomes can be sliced by it ("win rate when iv_rank > 70 vs below").

Two pieces:
  capture_entry_context(ctx) — a small snapshot taken in enter(), using ONLY
    the Context read surface, so it is identical across backtest / paper / live.
  attribution(trades, key, bins) — pure bucketed win-rate / avg-P&L of closed
    trades by one entry_context key. Numeric keys bucket by `bins` edges;
    categoricals bucket by distinct value.

Best-effort and defensive: attribution must never affect trading — a failed
read is simply omitted from the snapshot (invariant #6).
"""

from __future__ import annotations

from typing import Optional


_CHAIN_KEYS = ("pcr_oi", "atm_iv", "iv_skew", "max_pain")


def capture_entry_context(ctx) -> dict:
    """Snapshot the data state at entry. Keys are omitted when unavailable, so
    a backtest window with no recorded chain/IV simply yields a thinner dict —
    honest, and attribution() just puts those trades in an 'unknown' bucket."""
    snap: dict = {}
    try:
        snap["tod"] = ctx.now.strftime("%H:%M")
    except Exception:
        pass
    try:
        ivr = ctx.iv_rank()
        if ivr is not None:
            snap["iv_rank"] = round(ivr, 1)
    except Exception:
        pass
    try:
        b = ctx.signal("index_bias")
        if b and b.get("score") is not None:
            snap["index_bias"] = b["score"]
    except Exception:
        pass
    try:
        c = ctx.chain()
        if c:
            for k in _CHAIN_KEYS:
                if c.get(k) is not None:
                    snap[k] = round(c[k], 3)
    except Exception:
        pass
    return snap


def _bucket(value, bins) -> str:
    """Label `value` by the half-open interval [edge_i, edge_{i+1}) it falls in.
    Below the first edge -> '<e0'; at/above the last -> '>=e_last'."""
    if value < bins[0]:
        return f"<{bins[0]:g}"
    for i in range(len(bins) - 1):
        if bins[i] <= value < bins[i + 1]:
            return f"{bins[i]:g}-{bins[i + 1]:g}"
    return f">={bins[-1]:g}"


def attribution(trades: list, key: str, bins: Optional[list] = None) -> dict:
    """Bucketed outcome stats for closed trades by one entry_context `key`.

    trades: [{"entry_context": {...}, "pnl": float}, ...]
    bins:   sorted numeric edges for a numeric key (e.g. [0, 30, 70, 100]);
            omit for a categorical key (buckets by distinct value). Trades
            missing the key land in an 'unknown' bucket, so nothing is hidden.
    Returns {bucket: {n, wins, win_rate, avg_pnl, total_pnl}}, plus 'overall'.
    """
    buckets: dict = {}

    def _row(label, pnl):
        b = buckets.setdefault(label, {"n": 0, "wins": 0, "total_pnl": 0.0})
        b["n"] += 1
        b["wins"] += 1 if pnl > 0 else 0
        b["total_pnl"] += pnl

    tot = {"n": 0, "wins": 0, "total_pnl": 0.0}
    for t in trades:
        pnl = t.get("pnl", 0.0)
        ctxd = t.get("entry_context") or {}
        val = ctxd.get(key)
        if val is None:
            label = "unknown"
        elif bins is not None:
            try:
                label = _bucket(float(val), bins)
            except (TypeError, ValueError):
                label = "unknown"
        else:
            label = str(val)
        _row(label, pnl)
        tot["n"] += 1
        tot["wins"] += 1 if pnl > 0 else 0
        tot["total_pnl"] += pnl

    def _finish(b):
        return {"n": b["n"], "wins": b["wins"],
                "win_rate": round(100 * b["wins"] / b["n"], 1) if b["n"] else 0.0,
                "avg_pnl": round(b["total_pnl"] / b["n"], 2) if b["n"] else 0.0,
                "total_pnl": round(b["total_pnl"], 2)}

    out = {lab: _finish(b) for lab, b in buckets.items()}
    out["overall"] = _finish(tot)
    return out
