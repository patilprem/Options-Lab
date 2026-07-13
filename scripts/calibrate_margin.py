"""
Margin calibration (M5)
=======================
Compares fills.estimate_margin against Dhan's real margin API for common short
structures, per underlying, and writes a correction factor to registry settings
(`margin_factor:<underlying>`). The backtest engine reads that factor so its
capital policing tracks live SPAN.

    venv/Scripts/python -m scripts.calibrate_margin            # all index underlyings
    venv/Scripts/python -m scripts.calibrate_margin NIFTY      # one

Run during MARKET HOURS — Dhan computes derivative (SPAN) margin only when the
market is open; off-market every leg errors and the factor is left unchanged.
"""

from __future__ import annotations

import statistics
import sys

from app.core import registry
from app.core.contract import Action
from app.data import dhan_client as dc
from app.data.dhan_client import UNDERLYINGS
from app.engines import fills as F, margin as M
from app.engines.backtest import LOT_SIZES


def _chain(client, cfg):
    el = client.expiry_list(under_security_id=cfg["security_id"],
                            under_exchange_segment=cfg["segment"])
    expiry = el["data"]["data"][0]
    inner = client.option_chain(under_security_id=cfg["security_id"],
                                under_exchange_segment=cfg["segment"],
                                expiry=expiry)["data"]["data"]
    return float(inner["last_price"]), inner["oc"]


def _leg(oc, strike, side, action):
    node = oc[strike][side]
    return {"security_id": str(node["security_id"]), "action": action,
            "price": float(node["last_price"])}


def _structures(oc, spot, step):
    """Common short structures as leg lists. Strikes chosen ATM-relative."""
    keys = sorted(oc, key=float)
    atm = min(keys, key=lambda s: abs(float(s) - spot))
    i = keys.index(atm)

    def k(off):
        j = i + off
        return keys[j] if 0 <= j < len(keys) else None

    out = {}
    if k(0):
        out["short_straddle"] = [_leg(oc, k(0), "ce", Action.SELL),
                                 _leg(oc, k(0), "pe", Action.SELL)]
    if k(2) and k(-2):
        out["short_strangle"] = [_leg(oc, k(2), "ce", Action.SELL),
                                 _leg(oc, k(-2), "pe", Action.SELL)]
    if k(1) and k(-1) and k(4) and k(-4):
        out["iron_condor"] = [_leg(oc, k(1), "ce", Action.SELL),
                              _leg(oc, k(-1), "pe", Action.SELL),
                              _leg(oc, k(4), "ce", Action.BUY),
                              _leg(oc, k(-4), "pe", Action.BUY)]
    return out


def calibrate(underlying: str, client) -> float | None:
    cfg = UNDERLYINGS[underlying]
    lot = LOT_SIZES.get(underlying, 75)
    spot, oc = _chain(client, cfg)
    strikes = sorted(float(s) for s in oc)
    step = min((b - a for a, b in zip(strikes, strikes[1:]) if b > a), default=50)
    structures = _structures(oc, spot, step)

    print(f"\n=== {underlying}  spot={spot:.1f} lot={lot} step={step:g} ===")
    ratios = []
    for name, legs in structures.items():
        for leg in legs:
            leg["qty_units"] = lot
        est = F.estimate_margin([(l["price"], l["action"], lot) for l in legs],
                                spot, lot)  # factor 1.0 baseline
        try:
            real = sum(M.api_leg_margin(client, l["security_id"], l["action"], lot,
                                        l["price"], cfg["fno_segment"]) for l in legs)
        except Exception as e:
            print(f"  {name:15} estimate={est:>12,.0f}  real=FAILED ({e})")
            continue
        ratio = real / est if est else None
        print(f"  {name:15} estimate={est:>12,.0f}  real={real:>12,.0f}  ratio={ratio:.3f}")
        # naked shorts calibrate the estimate's short-notional %; hedged
        # structures get real SPAN benefit the flat estimate can't model.
        if name in ("short_straddle", "short_strangle") and ratio:
            ratios.append(ratio)

    if not ratios:
        print(f"  -> no usable real margins (market closed?); factor unchanged")
        return None
    factor = round(statistics.median(ratios), 4)
    registry.set_setting(f"margin_factor:{underlying}", str(factor))
    print(f"  -> wrote margin_factor:{underlying} = {factor}")
    return factor


def main():
    registry.init_db()
    client = dc.get_client()
    targets = sys.argv[1:] or [u for u, c in UNDERLYINGS.items()
                               if c.get("fno_segment")]
    for u in targets:
        if u not in UNDERLYINGS:
            print(f"unknown underlying {u}; skipping")
            continue
        try:
            calibrate(u, client)
        except Exception as e:
            print(f"{u}: calibration error: {e!r}")


if __name__ == "__main__":
    main()
