"""
Real Margin (M5)
================
Paper/live capital policing uses Dhan's real margin instead of the rough
`fills.estimate_margin` SPAN stand-in, with three safety nets:

  1. Per-leg SDK `margin_calculator` summed across the structure (conservative —
     no cross-leg SPAN offset, so it never *under*-states margin).
  2. A short TTL cache (margins barely move intraday; entries are infrequent).
  3. Graceful fallback to the calibrated estimate on ANY failure or when no
     live client is available (synthetic/dev, expired token, off-market SPAN).

Backtests keep using `fills.estimate_margin`, but scaled by a per-underlying
correction `factor` calibrated against the API (scripts/calibrate_margin.py →
settings `margin_factor:<underlying>`), so backtest capital policing tracks
live SPAN.

NOTE: Dhan's derivative (SPAN) margin only computes during market hours; the
equity margin path is live-verified, FNO wants a market-hours run to confirm
the real numbers. Until then the fallback runs and everything stays correct.
"""

from __future__ import annotations

import time
from typing import Optional

from app.core import registry
from app.core.contract import Action
from app.engines import fills as F

_CACHE: dict[tuple, tuple[float, float]] = {}   # key -> (margin, monotonic_ts)
_TTL = 300.0                                     # seconds


def underlying_factor(underlying: str) -> float:
    """Per-underlying SPAN correction for estimate_margin, from settings.
    Robust to a missing DB (backtests may run without registry init)."""
    try:
        raw = registry.setting(f"margin_factor:{underlying}", "")
        f = float(raw) if raw else 1.0
    except Exception:
        return 1.0
    return f if f > 0 else 1.0


def _act(action) -> str:
    return action.value if isinstance(action, Action) else str(action)


def api_leg_margin(client, security_id, action, qty_units: int, price: float,
                   segment: str = "NSE_FNO", product: str = "MARGIN") -> float:
    """One leg's real margin via the SDK. Raises on API failure."""
    r = client.margin_calculator(
        security_id=str(security_id), exchange_segment=segment,
        transaction_type=_act(action), quantity=int(abs(qty_units)),
        product_type=product, price=float(price))
    if r.get("status") == "success":
        return float((r.get("data") or {}).get("totalMargin") or 0.0)
    raise RuntimeError(r.get("remarks") or "margin_calculator failed")


def real_margin(legs: list[dict], spot: float, lot_size: int, *,
                underlying: str = "", client=None, segment: str = "NSE_FNO") -> float:
    """Margin for a multi-leg structure. `legs` items: {security_id, action,
    qty_units, price}. Returns the summed real per-leg margin (cached), or the
    calibrated estimate when a live client isn't available or the API fails.
    Never raises — capital policing must not crash a strategy."""
    est = F.estimate_margin(
        [(l["price"], l["action"], l["qty_units"]) for l in legs],
        spot, lot_size, factor=underlying_factor(underlying))

    if client is None or any(not l.get("security_id") for l in legs):
        return est

    key = tuple(sorted((str(l["security_id"]), _act(l["action"]), int(abs(l["qty_units"])))
                       for l in legs))
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[1] < _TTL:
        return hit[0]
    try:
        total = sum(api_leg_margin(client, l["security_id"], l["action"],
                                   l["qty_units"], l["price"], segment) for l in legs)
    except Exception as e:
        registry.record_event("warn", "engine",
                              f"real margin unavailable, using estimate: {e!r}")
        return est
    _CACHE[key] = (total, now)
    return total


def clear_cache() -> None:
    _CACHE.clear()
