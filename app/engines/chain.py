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


# The widest gap that can still separate two consecutive WEEKLY expiries. A
# holiday can skip a week (and festival clusters shift one), so 15 days still
# fits a real weekly cycle; a monthly-only underlying's next listed expiry
# never does.
WEEKLY_GAP_MAX_DAYS = 15


def has_weekly_cycle(expiries: list[str],
                     max_gap_days: int = WEEKLY_GAP_MAX_DAYS) -> bool:
    """Does this expiry list actually advance WEEKLY at its front?

    Asks the DATA instead of keeping a table of which underlyings still have
    weeklies: NSE discontinued BANKNIFTY's, MCX options are monthly-only, and
    any hardcoded list goes stale the next time an exchange changes a rule.

    Fewer than two parseable expiries proves nothing, so this answers False —
    MONTHLY offset 0 resolves to the same single contract either way, and the
    conservative label is the one that doesn't invent a weekly cycle.
    """
    dates = []
    for e in sorted(expiries or []):
        try:
            dates.append(date.fromisoformat(e))
        except (TypeError, ValueError):
            continue
    if len(dates) < 2:
        return False
    return (dates[1] - dates[0]).days <= max_gap_days


def effective_targets(targets, expiries,
                      max_gap_days: int = WEEKLY_GAP_MAX_DAYS) -> tuple:
    """Rewrite WEEKLY targets to MONTHLY for an underlying with no weekly cycle.

    MarketHub.CHAIN_TARGETS asks every configured underlying for
    ("WEEKLY", 0) and ("WEEKLY", 1). For BANKNIFTY and the MCX names that is
    not a thing that exists, and resolve_expiry answers ("WEEKLY", 0) with
    their nearest MONTHLY expiry anyway — so their chains were cached,
    recorded and persisted under expiry_kind="WEEKLY" while actually holding
    monthly contracts, and offset 1 resolved ~2 months out and was skipped,
    leaving those underlyings with exactly one recorded expiry, mislabelled.

    Remapping up front fixes both halves: the stored label matches the
    contract, and MONTHLY offset 1 is a genuine next-month expiry worth
    recording. Offsets are preserved, and for a monthly-only list MONTHLY 0
    resolves to the very expiry WEEKLY 0 did — so this RE-LABELS what we
    record without RE-POINTING it at a different contract.
    """
    if has_weekly_cycle(expiries, max_gap_days):
        return tuple(targets)
    out, seen = [], set()
    for kind, off in targets:
        t = ("MONTHLY", off) if kind == "WEEKLY" else (kind, off)
        if t not in seen:
            seen.add(t)
            out.append(t)
    return tuple(out)


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
