"""
Scanner signal bridge (F6)
==========================
The one door between the FNO stock scanner (a LIVE, wall-clock engine) and
strategy code (which reaches the world only through Context). A strategy calls
`ctx.signal(name)`; the paper/live contexts delegate here; this module asks the
registered provider (the scanner engine) for the current read.

Deliberately tiny and dependency-free so contract.py / the engines can import
it without cycles. The provider is registered once at app start.

Honesty rule baked in: only the LIVE-time contexts (paper, live) consult this.
BacktestContext returns None for every signal — the scanner reflects the market
NOW, and there is no historical intraday scanner series to replay, so a
backtest must not pretend to have seen these signals (see docs/FNO_SCANNER_PLAN
'Backtesting honesty').
"""

from __future__ import annotations

from typing import Optional

_PROVIDER = None


def register(provider) -> None:
    """Register the signal provider (the scanner engine). Idempotent."""
    global _PROVIDER
    _PROVIDER = provider


def get_signal(underlying: str, name: str) -> Optional[dict]:
    """Current scanner read of `name` for `underlying`, or None if there is no
    provider or no data. Never raises — a signal outage must not crash a
    strategy (invariant #6)."""
    p = _PROVIDER
    if p is None:
        return None
    try:
        return p.signal_for(underlying, name)
    except Exception:
        return None


# Recognised signal names (documented for the prompt/example):
#   "index_bias" — NIFTY/BANKNIFTY constituent-weighted bias dict
#   "setup"      — this underlying's composite setup score dict
#   "tier1"      — this underlying's Tier-1 metrics (buildup/volume/price)
#   "tier2"      — this underlying's Tier-2 chain metrics (PCR/IV/skew/liquidity)
SIGNAL_NAMES = ("index_bias", "setup", "tier1", "tier2")
