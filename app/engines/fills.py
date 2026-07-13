"""
Fill Simulation + Indian Options Cost Model
===========================================
Used by BOTH engines so backtest and paper P&L are computed identically.

Paper/live: fill BUY at ask, SELL at bid (plus optional extra slippage).
Backtest:   minute data has no quotes, so fill at close +/- a spread model
            that widens with distance from ATM (far OTM options are thin).

Charges are configurable. Defaults CALIBRATED against Dhan's Transaction
Estimator on 2026-07-13 (NSE index option, turnover 6,734: buy 26.63 /
sell 36.53 — reproduced to the paisa). Per side unless noted:
  brokerage        flat Rs 20 per executed order
  STT              0.15% of premium on SELL side only
  exchange txn     0.0355% of premium (NSE txn + IPFT, as Dhan bills it)
  GST              18% on (brokerage + exchange txn)
  SEBI fee         Rs 10 per crore of premium turnover
  stamp duty       0.003% of premium on BUY side only
"""

from __future__ import annotations

from dataclasses import dataclass, field
from app.core.contract import Action, OptionQuote


@dataclass
class FeeConfig:
    brokerage_per_order: float = 20.0
    stt_sell_pct: float = 0.0015       # 0.15% of sell premium (Dhan, 2026-07)
    exchange_txn_pct: float = 0.000355  # NSE txn + IPFT bundled, Dhan's rate
    gst_pct: float = 0.18
    sebi_per_crore: float = 10.0
    stamp_buy_pct: float = 0.00003     # 0.003% on buy


@dataclass
class SlippageConfig:
    # extra slippage on top of bid/ask, as fraction of price
    extra_pct: float = 0.0
    # backtest spread model: half-spread = base + per_offset * |strike_offset|
    bt_half_spread_base_pct: float = 0.0015   # 0.15% near ATM
    bt_half_spread_per_offset_pct: float = 0.0010


@dataclass
class FillResult:
    price: float
    fees: float
    notes: str = ""


def charges(premium_turnover: float, action: Action, cfg: FeeConfig) -> float:
    """Total statutory + brokerage cost for ONE order of given turnover.
    Components round to the paisa before summing and GST applies to
    brokerage + exchange txn only — matching how Dhan's estimator computes,
    so totals reproduce their buy/sell numbers exactly."""
    exch = round(premium_turnover * cfg.exchange_txn_pct, 2)
    sebi = round(premium_turnover / 1e7 * cfg.sebi_per_crore, 2)
    stt = round(premium_turnover * cfg.stt_sell_pct, 2) if action == Action.SELL else 0.0
    stamp = round(premium_turnover * cfg.stamp_buy_pct, 2) if action == Action.BUY else 0.0
    gst = round((cfg.brokerage_per_order + exch) * cfg.gst_pct, 2)
    return cfg.brokerage_per_order + exch + sebi + stt + stamp + gst


def fill_live(quote: OptionQuote, action: Action, qty_units: int,
              fee_cfg: FeeConfig, slip_cfg: SlippageConfig) -> FillResult:
    """Paper engine: cross the spread like a market order would."""
    if action == Action.BUY:
        px = quote.ask if quote.ask else quote.ltp
        px *= (1 + slip_cfg.extra_pct)
    else:
        px = quote.bid if quote.bid else quote.ltp
        px *= (1 - slip_cfg.extra_pct)
    turnover = px * abs(qty_units)
    return FillResult(round(px, 2), round(charges(turnover, action, fee_cfg), 2),
                      "live bid/ask fill")


def fill_backtest(close_price: float, strike_offset: int, action: Action,
                  qty_units: int, fee_cfg: FeeConfig, slip_cfg: SlippageConfig) -> FillResult:
    """Backtest: synthetic half-spread grows with distance from ATM."""
    half = slip_cfg.bt_half_spread_base_pct + \
        slip_cfg.bt_half_spread_per_offset_pct * abs(strike_offset)
    px = close_price * (1 + half) if action == Action.BUY else close_price * (1 - half)
    turnover = px * abs(qty_units)
    return FillResult(round(px, 2), round(charges(turnover, action, fee_cfg), 2),
                      f"bt fill, half-spread={half:.4%}")


def levels_for(entry_price: float, qty: int,
               sl_pct: float | None, target_pct: float | None):
    """Direction-aware premium levels. Short leg: stop above entry,
    target below. Long leg: stop below entry, target above."""
    short = qty < 0
    sl = tgt = None
    if sl_pct is not None:
        sl = round(entry_price * (1 + sl_pct) if short else entry_price * (1 - sl_pct), 2)
    if target_pct is not None:
        tgt = round(entry_price * (1 - target_pct) if short else entry_price * (1 + target_pct), 2)
    return sl, tgt


def level_hit(qty: int, mtm: float, stop_loss: float | None, target: float | None):
    """Returns 'stop_loss' | 'target' | None for current mark price."""
    short = qty < 0
    if stop_loss is not None and ((short and mtm >= stop_loss) or (not short and mtm <= stop_loss)):
        return "stop_loss"
    if target is not None and ((short and mtm <= target) or (not short and mtm >= target)):
        return "target"
    return None


# ---------------------------------------------------------------------------
# Margin estimation
# ---------------------------------------------------------------------------

def estimate_margin(legs_premium_and_action: list[tuple[float, Action, int]],
                    spot: float, lot_size: int,
                    short_margin_pct_of_notional: float = 0.12,
                    factor: float = 1.0) -> float:
    """VERY rough SPAN stand-in used to police capital allocation.

    Long option  -> margin = premium paid
    Short option -> margin ~ % of notional (default 12%), scaled by `factor`

    `factor` is a per-underlying correction calibrated against Dhan's real
    margin API (see engines/margin.py + scripts/calibrate_margin.py) so the
    backtest's capital policing matches live SPAN more closely. Paper/live use
    the real API directly (margin.real_margin) with this as the fallback.
    """
    total = 0.0
    for premium, action, qty_units in legs_premium_and_action:
        if action == Action.BUY:
            total += premium * abs(qty_units)
        else:
            total += spot * abs(qty_units) * short_margin_pct_of_notional * factor
    return total
