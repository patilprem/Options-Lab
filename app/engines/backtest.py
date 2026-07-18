"""
Backtest Engine
===============
Event-driven: replays stored underlying candles, calls strategy.on_bar(),
fills entries/exits through the shared fill simulator, marks positions to
market every bar, force-flattens at expiry close, and reports date-by-date
P&L plus summary metrics.

The strategy code is identical to what the paper engine runs — only the
Context implementation differs.
"""

from __future__ import annotations

import bisect
import math
import uuid
from collections import defaultdict
from datetime import date, datetime, time
from typing import Optional

from app.core.contract import (Action, Bar, Context, LegSpec, OptionQuote,
                               OptionType, Position, Strategy)
from app.engines import fills as F
from app.engines import margin as M
from app.engines.replay import SignalReplay

# Lot sizes by effective date (new-series dates from NSE/BSE circulars —
# SEBI's contract-value band forces periodic revisions, so a flat constant
# mis-sizes any backtest that crosses a revision). lot_size_on() picks the
# lot in force on a given day. Transition nuance is ignored (far-month
# contracts briefly kept old lots around each change) — irrelevant for the
# weekly ATM trading this platform does. MCX commodities are not listed;
# they fall back to the default of 50.
LOT_BASE = {"NIFTY": 25, "BANKNIFTY": 15, "FINNIFTY": 25, "MIDCPNIFTY": 50,
            "NIFTYNXT50": 10, "SENSEX": 10, "BANKEX": 15}
LOT_HISTORY: dict[str, list[tuple[str, int]]] = {
    # SEBI min-contract-value hike, new series from 2024-11-20
    # NSE FAOP64625: BANKNIFTY/MIDCPNIFTY up, new contracts from 2025-04-25
    # NSE FAOP70616: Jan-2026 series cuts, contracts created after 2025-12-30
    "NIFTY":      [("2024-11-20", 75), ("2025-12-31", 65)],
    "BANKNIFTY":  [("2024-11-20", 30), ("2025-04-25", 35), ("2025-12-31", 30)],
    "FINNIFTY":   [("2024-11-20", 65), ("2025-12-31", 60)],
    "MIDCPNIFTY": [("2024-11-20", 120), ("2025-04-25", 140), ("2025-12-31", 120)],
    "NIFTYNXT50": [("2024-11-20", 25)],
    "SENSEX":     [("2024-11-20", 20)],
    "BANKEX":     [("2024-11-20", 30)],
}


def lot_size_on(underlying: str, on: date | None = None) -> int:
    """Contract lot size in force on `on` (default: today)."""
    day = (on or date.today()).isoformat()
    lot = LOT_BASE.get(underlying, 50)
    for eff, sz in LOT_HISTORY.get(underlying, []):
        if eff <= day:
            lot = sz
    return lot


EOD = time(15, 25)


class BacktestContext(Context):
    def __init__(self, underlying: str, store, capital: float,
                 fee_cfg: F.FeeConfig, slip_cfg: F.SlippageConfig):
        self.underlying = underlying
        self.store = store
        self._capital = capital
        self.fee_cfg, self.slip_cfg = fee_cfg, slip_cfg
        self._margin_factor = M.underlying_factor(underlying)  # M5 SPAN calibration

        self._end: Optional[datetime] = None       # set by run_backtest; enables preload
        self._series: dict = {}                     # leg-key -> (ts_list, rows) cache
        self._contract: dict = {}                   # (strike,type,expiry) -> (ts_list, rows)
        self._replay = SignalReplay(store)          # F6: point-in-time signal replay
        self._warmup: list[Bar] = []                # pre-`start` bars for indicator lookback
        self._bars: list[Bar] = []
        self._positions: list[Position] = []
        self.closed: list[Position] = []
        self._margin_used = 0.0
        self._realized_by_day: dict[str, float] = defaultdict(float)
        self._fees_by_day: dict[str, float] = defaultdict(float)
        self.logs: list[str] = []
        self.fills_this_bar: list[Position] = []

    # -- engine wiring ------------------------------------------------------
    def _quote(self, ts: datetime, leg: LegSpec) -> Optional[OptionQuote]:
        """Mark-to-market lookup. Over a fixed backtest window this is called
        per position per bar; hitting DuckDB each time dominates runtime, so we
        preload each option's full series once and bisect for the latest ts<=now.
        Falls back to the store when preload isn't available (synthetic, tests)."""
        if self._end is None or not hasattr(self.store, "option_series"):
            return self.store.option_close(self.underlying, ts, leg)
        key = (leg.expiry_kind.value, leg.expiry_offset,
               leg.strike_offset, leg.option_type.value)
        cached = self._series.get(key)
        if cached is None:
            rows = self.store.option_series(self.underlying, *key, self._end)
            cached = ([r[0] for r in rows], rows)   # (ascending ts_list, rows)
            self._series[key] = cached
        ts_list, rows = cached
        i = bisect.bisect_right(ts_list, ts) - 1
        if i < 0:
            return None
        r = rows[i]     # (ts, close, strike, expiry, iv, oi)
        return OptionQuote(ts, self.underlying, r[3], r[2], leg.option_type,
                           ltp=r[1], iv=r[4], oi=r[5])

    def mark_price(self, ts: datetime, p: Position) -> Optional[float]:
        """Mark an OPEN position at its actual contract (fixed strike/expiry),
        NOT at its ATM-relative leg key. The stored offsets re-anchor to the
        current ATM every bar, so leg-key marking silently re-prices the
        position to a different contract — a directional winner never shows
        its gains. When the contract drifts outside the recorded ±2 offset
        window (big trend day), fall back to intrinsic + last-seen extrinsic."""
        if self._end is None or not hasattr(self.store, "option_series_by_strike"):
            q = self.store.option_close(self.underlying, ts, p.leg)
            return q.ltp if q else None
        key = (p.strike, p.leg.option_type.value,
               p.leg.expiry_kind.value, p.leg.expiry_offset)
        cached = self._contract.get(key)
        if cached is None:
            rows = self.store.option_series_by_strike(
                self.underlying, p.strike, p.leg.option_type.value,
                p.leg.expiry_kind.value, p.leg.expiry_offset, self._end)
            cached = ([r[0] for r in rows], rows)
            self._contract[key] = cached
        ts_list, rows = cached
        i = bisect.bisect_right(ts_list, ts) - 1
        if i < 0:
            return None
        last_ts, last_close = rows[i][0], rows[i][1]
        is_call = p.leg.option_type == OptionType.CALL
        intrinsic_now = max((self.spot - p.strike) if is_call else (p.strike - self.spot), 0.0)
        if (ts - last_ts).total_seconds() <= 600:      # fresh quote — trust it
            return last_close
        # stale: contract left the recorded window; estimate = intrinsic now
        # + extrinsic when last seen (conservative, decays to intrinsic floor)
        spot_last = next((b.close for b in reversed(self._bars) if b.ts <= last_ts),
                         self.spot)
        intrinsic_last = max((spot_last - p.strike) if is_call else (p.strike - spot_last), 0.0)
        extrinsic_last = max(last_close - intrinsic_last, 0.0)
        return max(round(intrinsic_now + extrinsic_last, 2), 0.05)

    def push_bar(self, bar: Bar) -> None:
        self._bars.append(bar)
        self.fills_this_bar = []
        for p in self._positions:
            m = self.mark_price(bar.ts, p)
            if m is not None:
                p.mtm_price = m
        for p in list(self.positions):
            hit = F.level_hit(p.qty, p.mtm_price, p.stop_loss, p.target)
            if hit:
                self.log(f"{hit} hit on {p.tag or p.id} @ {p.mtm_price}")
                self._close(p, reason=hit)

    # -- Context interface ---------------------------------------------------
    @property
    def now(self) -> datetime: return self._bars[-1].ts
    @property
    def spot(self) -> float: return self._bars[-1].close
    @property
    def lot_size(self) -> int:
        # date-aware: a replay crossing a lot revision sizes each fill with
        # the lot in force on that trade date
        return lot_size_on(self.underlying, self.now.date() if self._bars else None)

    def option(self, leg: LegSpec) -> Optional[OptionQuote]:
        return self._quote(self.now, leg)

    def history(self, n: int) -> list[Bar]:
        # warmup bars (recorded before `start`) prepend the live window so an
        # indicator strategy has lookback from its very first on_bar — see
        # _seed_warmup / run_backtest(warmup_bars=...).
        if self._warmup:
            return (self._warmup + self._bars)[-n:]
        return self._bars[-n:]

    def signal(self, name: str):
        """Point-in-time replay of recorded scanner signals (F6). Returns the
        value AS-OF the simulated clock, reconstructed from data the platform
        genuinely recorded live (index_bias_history, chain_snapshots), or None
        when nothing was recorded for this name/underlying near this time —
        strategies treat None as 'unknown'. Only signals backed by a real
        recorded time series are replayed; tier1/setup (per-stock) stay None.
        See app/engines/replay.py 'Backtesting honesty, revised'."""
        if not self._bars:
            return None
        return self._replay.signal(self.underlying, name, self.now)

    @property
    def positions(self) -> list[Position]:
        return [p for p in self._positions if p.is_open]

    @property
    def allocated_capital(self) -> float: return self._capital
    @property
    def available_capital(self) -> float: return self._capital - self._margin_used

    @property
    def day_pnl(self) -> float:
        d = self.now.date().isoformat()
        unreal = sum(p.unrealized_pnl for p in self.positions)
        return self._realized_by_day[d] + unreal

    def enter(self, legs: list[LegSpec], tag: str = "",
              sl_pct=None, target_pct=None) -> bool:
        quotes = [(leg, self.option(leg)) for leg in legs]
        if any(q is None for _, q in quotes):
            self.log(f"enter rejected: missing quote ({tag})")
            return False
        est = F.estimate_margin(
            [(q.ltp, leg.action, leg.lots * self.lot_size) for leg, q in quotes],
            self.spot, self.lot_size, factor=self._margin_factor)
        if est > self.available_capital:
            self.log(f"enter rejected: margin {est:,.0f} > available "
                     f"{self.available_capital:,.0f} ({tag})")
            return False
        margin_share = est / max(1, len(quotes))
        for leg, q in quotes:
            units = leg.lots * self.lot_size
            res = F.fill_backtest(q.ltp, leg.strike_offset, leg.action,
                                  units, self.fee_cfg, self.slip_cfg)
            qty = units if leg.action == Action.BUY else -units
            sl, tgt = F.levels_for(res.price, qty, sl_pct, target_pct)
            pos = Position(id=str(uuid.uuid4())[:8], leg=leg,
                           underlying=self.underlying, expiry=q.expiry,
                           strike=q.strike, qty=qty,
                           entry_price=res.price, entry_ts=self.now,
                           mtm_price=res.price, fees_paid=res.fees,
                           tag=tag or leg.tag, stop_loss=sl, target=tgt,
                           margin_blocked=round(margin_share, 2))
            self._positions.append(pos)
            self.fills_this_bar.append(pos)
            self._fees_by_day[self.now.date().isoformat()] += res.fees
        self._margin_used += est
        return True

    def exit(self, position_id: str, reason: str = "signal") -> bool:
        for p in self._positions:
            if p.id == position_id and p.is_open:
                self._close(p, reason=reason)
                return True
        return False

    def exit_all(self, reason: str = "signal") -> None:
        for p in list(self.positions):
            self._close(p, reason=reason)

    def set_levels(self, position_id: str, stop_loss=None, target=None) -> bool:
        for p in self._positions:
            if p.id == position_id and p.is_open:
                if stop_loss is not None:
                    p.stop_loss = stop_loss
                if target is not None:
                    p.target = target
                return True
        return False

    def _close(self, p: Position, reason: str = "signal") -> None:
        m = self.mark_price(self.now, p)      # exit the ACTUAL contract held
        price = m if m is not None else p.mtm_price
        action = Action.SELL if p.qty > 0 else Action.BUY
        res = F.fill_backtest(price, p.leg.strike_offset, action,
                              abs(p.qty), self.fee_cfg, self.slip_cfg)
        p.exit_price, p.exit_ts = res.price, self.now
        p.fees_paid += res.fees
        d = self.now.date().isoformat()
        self._realized_by_day[d] += p.realized_pnl
        self._fees_by_day[d] += res.fees
        p.exit_reason = reason
        self.closed.append(p)
        self._positions.remove(p)
        self._recompute_margin()

    def _recompute_margin(self) -> None:
        self._margin_used = F.estimate_margin(
            [(p.mtm_price, Action.BUY if p.qty > 0 else Action.SELL, abs(p.qty))
             for p in self.positions], self.spot, self.lot_size,
            factor=self._margin_factor)

    def log(self, msg: str) -> None:
        self.logs.append(f"[{self.now}] {msg}")


# ---------------------------------------------------------------------------

def _seed_warmup(ctx: "BacktestContext", store, underlying: str,
                 start: datetime, interval: int, n: int) -> None:
    """Preload the last `n` bars recorded BEFORE `start` into ctx warmup so an
    indicator strategy has lookback from its first real on_bar (instead of
    starting cold and trading blind until enough of the window has replayed).
    These bars are NEVER iterated (no on_bar / no fills / no daily P&L) — they
    exist only to deepen ctx.history()."""
    if n <= 0:
        return
    from datetime import timedelta
    per_day = max(1, 375 // max(1, interval))          # ~375 trading min/day
    lookback_days = max(5, (n // per_day + 2) * 2)     # pad for weekends/holidays
    prior = store.underlying_bars(underlying, start - timedelta(days=lookback_days),
                                  start, interval)
    prior = [b for b in prior if b.ts < start]
    if prior:
        ctx._warmup = prior[-n:]


def run_backtest(strategy: Strategy, store, start: datetime, end: datetime,
                 capital: float,
                 fee_cfg: F.FeeConfig | None = None,
                 slip_cfg: F.SlippageConfig | None = None,
                 warmup_bars: int = 0) -> dict:
    meta = strategy.meta()
    ctx = BacktestContext(meta.underlying, store, capital,
                          fee_cfg or F.FeeConfig(), slip_cfg or F.SlippageConfig())
    ctx._end = end                      # enables in-memory option-series preload
    interval = int(meta.timeframe)
    # warmup depth: explicit arg wins; else honor a strategy's declared hint so
    # existing backtests (no hint) keep their exact current behavior.
    if warmup_bars <= 0:
        warmup_bars = int((meta.params or {}).get("warmup_bars", 0) or 0)
    if warmup_bars > 0:
        _seed_warmup(ctx, store, meta.underlying, start, interval, warmup_bars)
    bars = store.underlying_bars(meta.underlying, start, end, interval)
    if not bars:
        return {"error": "no data in store for this range"}

    strategy_started = False
    equity = capital
    daily: dict[str, dict] = {}
    cur_day: Optional[str] = None

    for i, bar in enumerate(bars):
        d = bar.ts.date().isoformat()
        if cur_day and d != cur_day:
            _close_day(ctx, strategy, cur_day, daily, equity)
            equity = daily[cur_day]["equity_eod"]
        cur_day = d

        ctx.push_bar(bar)
        if not strategy_started:
            strategy.on_start(ctx)
            strategy_started = True

        # expiry-day force flatten near close (backfilled expiry can be NULL —
        # those contracts are intraday-managed by the strategy/day-end exit)
        if bar.ts.time() >= EOD:
            for p in list(ctx.positions):
                if p.expiry is not None and p.expiry <= bar.ts.date():
                    ctx._close(p, reason='expiry')

        try:
            strategy.on_bar(ctx, bar)
        except Exception as e:  # a buggy strategy shouldn't kill the engine
            ctx.log(f"STRATEGY ERROR: {e!r}")
            break
        for p in ctx.fills_this_bar:
            strategy.on_fill(ctx, p)

    if cur_day:
        ctx.exit_all(reason="squareoff")   # end-of-range flatten, not a signal
        _close_day(ctx, strategy, cur_day, daily, equity)
    strategy.on_stop(ctx)

    return _report(daily, ctx, capital)


def _close_day(ctx: BacktestContext, strategy: Strategy, day: str,
               daily: dict, equity_prev: float) -> None:
    strategy.on_day_end(ctx)
    realized = ctx._realized_by_day[day]
    unreal = sum(p.unrealized_pnl for p in ctx.positions)
    fees = ctx._fees_by_day[day]
    daily[day] = {
        "date": day, "realized": round(realized, 2),
        "unrealized_eod": round(unreal, 2), "fees": round(fees, 2),
        "equity_eod": round(equity_prev + realized + unreal
                            - daily.get(day, {}).get("unreal_prev", 0), 2),
        "trades": sum(1 for p in ctx.closed if p.exit_ts
                      and p.exit_ts.date().isoformat() == day),
    }


def _report(daily: dict, ctx: BacktestContext, capital: float) -> dict:
    days = [daily[k] for k in sorted(daily)]
    eq = [capital] + [d["equity_eod"] for d in days]
    rets = [(eq[i] - eq[i - 1]) / eq[i - 1] for i in range(1, len(eq)) if eq[i - 1] > 0]
    peak, max_dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak if peak else 0)
    total_pnl = eq[-1] - capital
    wins = [p for p in ctx.closed if p.realized_pnl > 0]
    sharpe = 0.0
    if len(rets) > 1:
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
        sharpe = (mu / sd) * math.sqrt(252) if sd > 0 else 0.0
    return {
        "summary": {
            "capital": capital,
            "total_pnl": round(total_pnl, 2),
            "return_pct": round(100 * total_pnl / capital, 2) if capital else 0,
            "max_drawdown_pct": round(100 * max_dd, 2),
            "sharpe": round(sharpe, 2),
            "n_trades": len(ctx.closed),
            "win_rate_pct": round(100 * len(wins) / len(ctx.closed), 1) if ctx.closed else 0,
            "total_fees": round(sum(p.fees_paid for p in ctx.closed), 2),
        },
        "daily": days,   # <-- your date-by-date performance
        "logs": ctx.logs[-200:],
    }
