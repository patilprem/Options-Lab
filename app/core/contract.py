"""
Strategy Contract
=================
Every strategy (LLM-generated or hand-written) MUST subclass `Strategy`.
The platform never tries to "understand" arbitrary code — it only talks
to strategies through this fixed interface. The SAME strategy object runs
unmodified in both the backtest engine and the live paper engine; only
the Context implementation behind it changes.

Strikes are always expressed RELATIVE to spot ("ATM", "ATM+2", "ATM-1")
so the same definition works across backtests (Dhan expired-options API
is ATM-relative) and live trading (resolved against the live chain).
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional


# ---------------------------------------------------------------------------
# Enums / value objects
# ---------------------------------------------------------------------------

class OptionType(str, enum.Enum):
    CALL = "CALL"
    PUT = "PUT"


class Action(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class ExpiryKind(str, enum.Enum):
    WEEKLY = "WEEKLY"    # nearest weekly expiry (offset 0 = current week)
    MONTHLY = "MONTHLY"  # nearest monthly expiry


@dataclass(frozen=True)
class LegSpec:
    """A single option leg, defined relative to spot.

    strike_offset: 0 = ATM, +2 = 2 strikes above ATM, -3 = 3 strikes below.
    expiry_offset: 0 = nearest expiry of that kind, 1 = next one, etc.
    """
    option_type: OptionType
    action: Action
    strike_offset: int = 0
    expiry_kind: ExpiryKind = ExpiryKind.WEEKLY
    expiry_offset: int = 0
    lots: int = 1
    tag: str = ""  # strategy's own label, e.g. "short_ce", "hedge_pe"


@dataclass
class Bar:
    """One candle of the UNDERLYING (spot or future)."""
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    oi: float = 0.0


@dataclass
class OptionQuote:
    """Snapshot of one option instrument."""
    ts: datetime
    underlying: str
    expiry: date
    strike: float
    option_type: OptionType
    ltp: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    iv: Optional[float] = None
    oi: Optional[float] = None
    volume: Optional[float] = None
    delta: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    gamma: Optional[float] = None
    security_id: Optional[str] = None    # live contract id (for real margin/orders)


@dataclass
class Position:
    """An open (or closed) leg position held by a strategy instance."""
    id: str
    leg: LegSpec
    underlying: str
    expiry: date
    strike: float
    qty: int                       # signed: + long, - short (in units, lots*lot_size)
    entry_price: float
    entry_ts: datetime
    exit_price: Optional[float] = None
    exit_ts: Optional[datetime] = None
    mtm_price: float = 0.0
    fees_paid: float = 0.0
    tag: str = ""
    stop_loss: Optional[float] = None    # premium level; engine enforces if set
    target: Optional[float] = None       # premium level; engine enforces if set
    margin_blocked: float = 0.0          # estimated margin share for this leg
    exit_reason: str = ""                # entry|stop_loss|target|time_exit|signal|squareoff|expiry|pause|manual
    entry_context: dict = field(default_factory=dict)  # data state at entry (signal attribution)
    mfe: float = 0.0                     # max favourable excursion: best
                                         # unrealized ₹ P&L seen over the life
    mae: float = 0.0                     # max adverse excursion: worst (≤0)
                                         # unrealized ₹ P&L seen over the life

    def mark(self, price: float) -> None:
        """Mark to `price` and track the peak/trough unrealized P&L (MFE/MAE)
        over the position's life. Engines call this every bar instead of
        assigning mtm_price directly, so the excursion the trade actually
        offered is captured for journal reflection (targets too far? stops too
        tight?). Best-effort and side-effect-free beyond these three fields."""
        self.mtm_price = price
        u = (price - self.entry_price) * self.qty
        if u > self.mfe:
            self.mfe = u
        if u < self.mae:
            self.mae = u

    @property
    def is_open(self) -> bool:
        return self.exit_ts is None

    @property
    def unrealized_pnl(self) -> float:
        if not self.is_open:
            return 0.0
        return (self.mtm_price - self.entry_price) * self.qty

    @property
    def realized_pnl(self) -> float:
        if self.is_open:
            return 0.0
        return (self.exit_price - self.entry_price) * self.qty - self.fees_paid


@dataclass
class StrategyMeta:
    """Static metadata the platform reads BEFORE running the strategy."""
    name: str
    underlying: str                 # e.g. "NIFTY", "BANKNIFTY", "CRUDEOIL", "RELIANCE"
    segment: str = "NSE_FNO"        # NSE_FNO | BSE_FNO | MCX_FO
    timeframe: str = "5"            # candle interval in minutes: 1/5/15/25/60
    params: dict = field(default_factory=dict)  # tunable params (exposed in UI)
    description: str = ""


# ---------------------------------------------------------------------------
# Context — the ONLY door between a strategy and the outside world
# ---------------------------------------------------------------------------

class Context(abc.ABC):
    """Passed into every hook. Backtest and paper engines each provide
    their own implementation. Strategies must never import anything to
    reach market data or place orders — everything goes through here."""

    # ---- market data -----------------------------------------------------
    @property
    @abc.abstractmethod
    def now(self) -> datetime: ...

    @property
    @abc.abstractmethod
    def spot(self) -> float:
        """Latest underlying price."""

    @abc.abstractmethod
    def option(self, leg: LegSpec) -> Optional[OptionQuote]:
        """Resolve a relative leg to a live quote (None if unavailable)."""

    @abc.abstractmethod
    def history(self, n: int, interval: Optional[int] = None) -> list[Bar]:
        """Last n underlying bars, oldest first. `interval` (minutes) selects a
        DIFFERENT timeframe than the strategy's own — e.g. a 5-min strategy
        consulting the 60-min trend via history(50, interval=60). Higher
        timeframes are resampled from stored data; the strategy's own interval
        (default, or interval=None) returns its live in-memory bars."""

    def signal(self, name: str) -> Optional[dict]:
        """FNO-scanner read for THIS strategy's underlying (F6), or None.

        Names: "index_bias" (NIFTY/BANKNIFTY constituent-weighted bias),
        "setup" (this name's composite setup score), "tier1" (buildup / volume
        surge / price change), "tier2" (chain PCR / IV / skew / liquidity).

        Paper and live contexts return the live scanner read. The backtest
        context REPLAYS recorded point-in-time data as-of the simulated bar:
        "index_bias" (from index_bias_history) and "tier2" (from
        chain_snapshots) come back for days/times the recorder was running, and
        None otherwise; "tier1"/"setup" are not replayed (always None in
        backtest). So a signal is None whenever it is genuinely unknown —
        strategies must always treat None as "unknown", and know that a signal
        only backtests over the window it was actually recorded for.
        Default here is None so a context without scanner wiring is safe."""
        return None

    def chain(self) -> Optional[dict]:
        """Normalized option-chain summary for THIS strategy's underlying as-of
        now, or None if unavailable. Keys: pcr_oi, pcr_volume, atm_iv, iv_skew
        (avg OTM-put IV − OTM-call IV; positive = downside fear), call_oi,
        put_oi, max_pain (OI-gravity strike). Live/paper read the live chain
        cache; the backtest REPLAYS it from recorded chain_snapshots (None for
        windows the recorder wasn't running). Default None so a context without
        chain wiring is safe — always handle None."""
        return None

    def iv_rank(self, lookback_days: int = 30) -> Optional[float]:
        """Percentile rank (0..100) of the current ATM IV within its own last
        `lookback_days` — the single most useful options-selling filter ("is
        premium rich right now?"). Backtest reads option_bars IV; live/paper
        read recorded chain_snapshots IV. None when there isn't enough IV
        history (early backtest windows, thin recording) — handle None.
        Default None so a context without IV history is safe."""
        return None

    # ---- portfolio -------------------------------------------------------
    @property
    @abc.abstractmethod
    def positions(self) -> list[Position]:
        """Open positions of THIS strategy instance only."""

    @property
    @abc.abstractmethod
    def allocated_capital(self) -> float: ...

    @property
    @abc.abstractmethod
    def available_capital(self) -> float:
        """Allocated capital minus margin blocked by open positions."""

    @property
    @abc.abstractmethod
    def day_pnl(self) -> float: ...

    # ---- actions ---------------------------------------------------------
    @abc.abstractmethod
    def enter(self, legs: list[LegSpec], tag: str = "",
              sl_pct: Optional[float] = None,
              target_pct: Optional[float] = None) -> bool:
        """Open a multi-leg structure atomically. Returns False if the
        engine rejects it (paused, insufficient capital, no quote...).

        sl_pct / target_pct declare per-leg premium levels relative to the
        fill price (direction-aware: for a short leg the stop is ABOVE
        entry, for a long leg below). The engine records them, shows them
        on the dashboard, and enforces them as a safety net every bar —
        your strategy can still exit earlier with its own logic."""

    @abc.abstractmethod
    def set_levels(self, position_id: str,
                   stop_loss: Optional[float] = None,
                   target: Optional[float] = None) -> bool:
        """Update declared levels on an open position (e.g. trail a stop)."""

    @abc.abstractmethod
    def exit(self, position_id: str, reason: str = "signal") -> bool:
        """Close one open position. `reason` records WHY on the blotter for
        later exit-attribution analysis; default "signal" (your own decision).
        Pass a specific value like "time_exit" when it fits. The label
        "manual" is reserved for human intervention — don't use it here."""

    @abc.abstractmethod
    def exit_all(self, reason: str = "signal") -> None:
        """Close every open position. Same `reason` semantics as exit()."""

    @abc.abstractmethod
    def log(self, msg: str) -> None: ...


# ---------------------------------------------------------------------------
# The base class LLM-generated code must subclass
# ---------------------------------------------------------------------------

class Strategy(abc.ABC):
    """Subclass this. Implement `meta()` and `on_bar()` at minimum."""

    @abc.abstractmethod
    def meta(self) -> StrategyMeta: ...

    def on_start(self, ctx: Context) -> None:
        """Called once when the strategy is (re)started."""

    @abc.abstractmethod
    def on_bar(self, ctx: Context, bar: Bar) -> None:
        """Called on every closed candle of the underlying."""

    def on_tick(self, ctx: Context) -> None:
        """Optional: called on every tick batch (paper/live only)."""

    def on_fill(self, ctx: Context, position: Position) -> None:
        """Called when an entry/exit actually fills."""

    def on_day_end(self, ctx: Context) -> None:
        """Called at market close each day (both engines)."""

    def on_stop(self, ctx: Context) -> None:
        """Called when strategy is stopped. Positions may still be open."""
