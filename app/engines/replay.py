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

PERFORMANCE: a strategy may call ctx.signal(...) on every bar — over a
multi-year backtest that's tens of thousands of calls. The naive per-call
store query (index_bias_asof / chain_cache_asof) turns that into tens of
thousands of DuckDB round-trips serialized behind the store's single-connection
lock, which is slow enough to time out the backtest request entirely. Once
`set_end()` is called (BacktestContext does this as soon as the replay window
is known — same lifetime as its own option-series preload), each underlying's
full recorded series is fetched ONCE and bisected in memory after that —
exactly the pattern BacktestContext._quote()/mark_price() already use for
option series. Falls back to the live per-call store methods when set_end()
was never called (bare construction, e.g. in tests) or the store doesn't
support the bulk-preload methods (SyntheticStore).

Pure and defensive: a signal outage must never crash a strategy (invariant #6).
"""

from __future__ import annotations

import bisect
from datetime import datetime, timedelta
from typing import Optional


class SignalReplay:
    """Serves recorded scanner signals to the backtest context as-of a bar's
    timestamp. Reuses the store's point-in-time readers and the scanner's own
    pure metric functions so replayed values match what ran live."""

    def __init__(self, store):
        self.store = store
        self._end: Optional[datetime] = None       # set via set_end() once known
        self._bias_series: dict = {}                # index_name -> (ts_list, rows)
        self._chain_rows: dict = {}                  # underlying -> (ts_list, rows)

    def set_end(self, end: datetime) -> None:
        """Enables preload-and-bisect instead of a query per signal() call.
        Called once the backtest's replay window is known (mirrors
        BacktestContext._end)."""
        self._end = end

    def signal(self, underlying: str, name: str, ts: datetime) -> Optional[dict]:
        try:
            if name == "index_bias":
                return self._index_bias_at(underlying, ts)
            if name in ("tier2", "chain"):
                cache = self.chain_cache_at(underlying, ts)
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

    # -- index_bias (preload once, bisect after) ------------------------------

    def _index_bias_at(self, underlying: str, ts: datetime, max_age_min: int = 20):
        if self._end is not None and hasattr(self.store, "index_bias_full_series"):
            cached = self._bias_series.get(underlying)
            if cached is None:
                rows = self.store.index_bias_full_series(underlying, self._end)
                cached = ([r[0] for r in rows], rows)
                self._bias_series[underlying] = cached
            ts_list, rows = cached
            i = bisect.bisect_right(ts_list, ts) - 1
            if i < 0:
                return None
            r = rows[i]
            if r[1] is None or (ts - r[0]).total_seconds() > max_age_min * 60:
                return None
            score = r[1]
            label = ("bullish" if score > 0.3 else
                     "bearish" if score < -0.3 else "neutral")
            return {"score": score, "buildup_breadth": r[2], "price_breadth": r[3],
                    "bull_weight": r[4], "bear_weight": r[5], "coverage": r[6],
                    "n": r[7], "label": label, "spot": r[8], "contributors": [],
                    "as_of": r[0].isoformat(sep=" ", timespec="seconds"),
                    "replayed": True}
        if hasattr(self.store, "index_bias_asof"):
            return self.store.index_bias_asof(underlying, ts, max_age_min=max_age_min)
        return None

    # -- chain (preload once, bisect after) ------------------------------------

    def chain_cache_at(self, underlying: str, ts: datetime, max_age_min: float = 10.0):
        """Reconstruct the point-in-time hub-shaped chain cache ({(kind,
        offset, strike_offset, otype): OptionQuote}) chain_cache_asof would
        have returned, from a per-underlying preloaded row set. Public so
        BacktestContext.chain() can share this instead of duplicating the
        bisect/batch-window logic."""
        if self._end is not None and hasattr(self.store, "chain_snapshot_rows"):
            cached = self._chain_rows.get(underlying)
            if cached is None:
                rows = self.store.chain_snapshot_rows(underlying, self._end)
                cached = ([r[0] for r in rows], rows)
                self._chain_rows[underlying] = cached
            ts_list, rows = cached
            i = bisect.bisect_right(ts_list, ts) - 1
            if i < 0:
                return None
            latest = ts_list[i]
            if (ts - latest).total_seconds() > max_age_min * 60:
                return None
            # one poll's rows land within a few seconds of each other — same
            # 90s batch window chain_cache_asof's SQL used.
            lo = bisect.bisect_left(ts_list, latest - timedelta(seconds=90))
            cache = {}
            from app.core.contract import OptionQuote, OptionType
            for r in rows[lo:i + 1]:
                key = (r[2], r[3], r[5], r[6])   # kind, offset, strike_offset, otype
                cache[key] = OptionQuote(
                    r[0], underlying, r[1], r[4], OptionType(r[6]), ltp=r[7],
                    bid=r[8], ask=r[9], iv=r[10], oi=r[11], volume=r[12],
                    delta=r[13], theta=r[14], vega=r[15], gamma=r[16])
            return cache or None
        if hasattr(self.store, "chain_cache_asof"):
            return self.store.chain_cache_asof(underlying, ts, max_age_min=int(max_age_min))
        return None
