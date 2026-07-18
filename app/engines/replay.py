"""
Backtest Signal Replay (F6 — backtesting honesty, revised)
==========================================================
The FNO scanner is a LIVE, wall-clock engine, so for a long time the backtest
context returned None for every `ctx.signal(...)` — there was no way to know
what the scanner "would have said" on a past bar, and faking it is worse than
admitting ignorance.

That is only half true now: the platform ALREADY records genuine point-in-time
scanner data as it runs — `index_bias_history` (the constituent-weighted index
bias, timestamped every sweep) and `chain_snapshots` (the full live option
chain per poll). Those are real recorded time series, so replaying them AS-OF
the simulated clock is honest, not invented: at bar time T we return the most
recent reading recorded at or before T, or None when nothing was recorded near
then (older backtest windows, or days the recorder wasn't running).

What is NOT replayed: `tier1` / `setup`. Those are per-FNO-STOCK reads derived
from `stock_snapshots` with trailing volume baselines; index underlyings have
no tier1/setup live either, and a faithful stock replay needs baseline
recomputation per bar — deferred. They return None (unknown), exactly as before.

Pure and defensive: a signal outage must never crash a strategy (invariant #6).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional


class SignalReplay:
    """Serves recorded scanner signals to the backtest context as-of a bar's
    timestamp. Reuses the store's point-in-time readers and the scanner's own
    pure metric functions so replayed values match what ran live."""

    def __init__(self, store):
        self.store = store

    def signal(self, underlying: str, name: str, ts: datetime) -> Optional[dict]:
        try:
            if name == "index_bias":
                if not hasattr(self.store, "index_bias_asof"):
                    return None
                return self.store.index_bias_asof(underlying, ts)
            if name in ("tier2", "chain"):
                if not hasattr(self.store, "chain_cache_asof"):
                    return None
                cache = self.store.chain_cache_asof(underlying, ts)
                if not cache:
                    return None
                # SAME pure function the live Tier-2 loop uses, on a cache
                # rebuilt from the recorded chain → identical PCR/IV/skew shape.
                from app.engines.scanner import chain_metrics
                return chain_metrics(cache)
            # tier1 / setup / anything else: honestly unknown in replay.
            return None
        except Exception:
            return None
