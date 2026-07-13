"""
Option Chain Normalization + Expiry Resolution (M3)
===================================================
Turns a live DhanHQ option chain into ATM-relative OptionQuote objects keyed
exactly the way store.option_close resolves a LegSpec:

    (expiry_kind, expiry_offset, strike_offset, option_type)

so PaperContext fills can cross REAL bid/ask (and read greeks) through the same
lookup that backtests use against the store.

Pure functions only — the polling/rate-limiting lives in MarketHub. The live
SDK double-wraps the chain payload (resp["data"]["data"] = {last_price, oc});
normalize_chain descends that automatically so it also accepts a pre-descended
dict or a saved fixture.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from app.core.contract import OptionQuote, OptionType

# Cache key: (expiry_kind, expiry_offset, strike_offset, option_type_value)
ChainKey = tuple


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _strike_step(strikes: list[float]) -> float:
    """Smallest positive gap between consecutive strikes = the chain's grid."""
    gaps = sorted({round(b - a, 4) for a, b in zip(strikes, strikes[1:]) if b > a})
    return gaps[0] if gaps else 1.0


def normalize_chain(data: dict, underlying: str, expiry_kind: str,
                    expiry_offset: int, expiry_date: date,
                    ts: datetime) -> dict[ChainKey, OptionQuote]:
    """Chain payload -> {key: OptionQuote}. `data` may be the raw double-nested
    SDK payload, a pre-descended {last_price, oc}, or a saved fixture."""
    if isinstance(data.get("data"), dict) and "oc" in data["data"]:
        data = data["data"]
    spot = _f(data.get("last_price"))
    oc = data.get("oc") or {}
    if not spot or not oc:
        return {}

    strikes = sorted(float(k) for k in oc)
    step = _strike_step(strikes)
    atm = min(strikes, key=lambda s: abs(s - spot))  # nearest listed strike to spot

    out: dict[ChainKey, OptionQuote] = {}
    for k_str, node in oc.items():
        strike = float(k_str)
        strike_offset = int(round((strike - atm) / step))
        for side, otype in (("ce", OptionType.CALL), ("pe", OptionType.PUT)):
            leg = node.get(side) or {}
            if not leg:
                continue
            g = leg.get("greeks") or {}
            sid = leg.get("security_id")
            out[(expiry_kind, expiry_offset, strike_offset, otype.value)] = OptionQuote(
                ts=ts, underlying=underlying, expiry=expiry_date, strike=strike,
                option_type=otype,
                ltp=_f(leg.get("last_price")) or 0.0,
                bid=_f(leg.get("top_bid_price")),
                ask=_f(leg.get("top_ask_price")),
                iv=_f(leg.get("implied_volatility")),
                oi=_f(leg.get("oi")),
                volume=_f(leg.get("volume")),
                delta=_f(g.get("delta")), theta=_f(g.get("theta")),
                vega=_f(g.get("vega")), gamma=_f(g.get("gamma")),
                security_id=str(sid) if sid is not None else None)
    return out


def chain_spot(data: dict) -> Optional[float]:
    """Underlying last_price from a chain payload (raw double-nested or
    pre-descended) — recorded alongside snapshots for moneyness context."""
    if isinstance(data.get("data"), dict) and "oc" in data["data"]:
        data = data["data"]
    return _f(data.get("last_price"))


def resolve_expiry(expiries: list[str], expiry_kind: str, offset: int) -> Optional[str]:
    """Pick the ISO expiry for an ATM-relative (kind, offset) from a sorted-
    ascending expiry list. WEEKLY offset 0 = nearest; MONTHLY offset 0 = nearest
    month-end (the last expiry within each calendar month)."""
    if not expiries:
        return None
    if expiry_kind == "MONTHLY":
        by_month: dict[tuple, str] = {}
        for e in sorted(expiries):
            d = date.fromisoformat(e)
            by_month[(d.year, d.month)] = e  # ascending -> keeps the month's last
        monthly = [by_month[k] for k in sorted(by_month)]
        return monthly[offset] if 0 <= offset < len(monthly) else None
    weekly = sorted(expiries)
    return weekly[offset] if 0 <= offset < len(weekly) else None
