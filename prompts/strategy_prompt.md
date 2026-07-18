# Strategy Generation Prompt (paste this into your LLM, then describe your strategy)

You are writing a trading strategy for my options platform "OptionsLab".
Output ONLY Python code, no explanations, no markdown fences.

## Hard rules
1. Define EXACTLY ONE class that subclasses `Strategy`.
2. Do NOT write any import statements. These names are already available:
   `Strategy, Context, StrategyMeta, LegSpec, Bar, OptionQuote, Position,
   OptionType, Action, ExpiryKind`, and `indicators` (the tested toolbox
   below). (If you truly need it, only `math`, `statistics` and `datetime`
   may be imported — nothing else.)
3. Never touch files, network, threads, `eval`, `exec`, or `open`.
4. All market access and orders go through the `ctx` object ONLY.
5. Strikes are RELATIVE: `strike_offset=0` is ATM, `+2` is two strikes
   above ATM, `-1` is one below. Never use absolute strike numbers.
6. Expose every tunable number in `meta().params` and read from
   `self.params` so I can tweak them without regenerating code.
7. Keep per-bar work light: no heavy loops over long history each bar. Use
   the `indicators` toolbox (below) — do NOT hand-roll EMA/ATR/Supertrend/
   VWAP/pivots; the built-ins are tested and stateless (recomputed from the
   history window each call, so a restart can't desync them).
8. If your logic needs indicator lookback (EMA/RSI/ATR/etc.), set
   `"warmup_bars": N` in `meta().params` — the engine preloads N bars of
   history BEFORE the start of a backtest and on a mid-session paper restart,
   so `ctx.history(n)` is deep enough from your first `on_bar` instead of
   starting cold. Choose N ≥ your longest lookback (e.g. 60 for a 50-period
   indicator).

## The interface you must implement

```python
class MyStrategy(Strategy):
    def __init__(self):
        self.params = {"sl_pct": 0.30, "target_pct": 0.60}  # example

    def meta(self) -> StrategyMeta:
        return StrategyMeta(
            name="Short Straddle 9:20",
            underlying="NIFTY",          # NIFTY | BANKNIFTY | stock symbol
            segment="NSE_FNO",
            timeframe="5",               # candle minutes: 1/5/15/25/60
            params=self.params,
            description="one line",
        )

    def on_start(self, ctx): ...          # optional
    def on_bar(self, ctx, bar): ...       # REQUIRED — your main logic
    def on_fill(self, ctx, position): ... # optional
    def on_day_end(self, ctx): ...        # optional
    def on_stop(self, ctx): ...           # optional
```

## What `ctx` gives you

Read:  `ctx.now`, `ctx.spot`, `ctx.history(n) -> list[Bar]`,
       `ctx.history(n, interval=60)` — SAME underlying at a DIFFERENT (higher)
        timeframe, e.g. a 5-min strategy consulting the 60-min trend; resampled
        from stored data, returns [] if unavailable,
       `ctx.chain() -> dict | None` — option-chain summary NOW for your
        underlying: pcr_oi / pcr_volume / atm_iv / iv_skew (>0 = downside fear)
        / call_oi / put_oi / max_pain (OI-gravity strike). Live in paper/live,
        REPLAYED from recorded snapshots in backtest, None when unavailable,
       `ctx.iv_rank(lookback_days=30) -> float | None` — percentile (0..100) of
        current ATM IV in its recent range; the key premium-selling filter
        (sell rich IV, buy cheap). None without enough IV history,
       `ctx.option(LegSpec(...)) -> OptionQuote | None`
       (OptionQuote has ltp, bid, ask, iv, oi, delta/theta/vega/gamma
        — greeks may be None in backtests),
       `ctx.positions -> list[Position]` (only YOUR open positions;
        Position has entry_price, mtm_price, unrealized_pnl, tag, id),
       `ctx.allocated_capital`, `ctx.available_capital`, `ctx.day_pnl`,
       `ctx.signal(name) -> dict | None` — FNO-scanner read for your
        underlying. Names: "index_bias" (NIFTY/BANKNIFTY weighted breadth,
        dict has score in [-1,1] + label), "setup" (this name's composite
        setup score + bias CE/PE), "tier1" (buildup / volume_surge /
        price_change_pct), "tier2" (chain pcr_oi / atm_iv / iv_skew /
        liquidity). In paper/live these are the live read. In BACKTEST,
        "index_bias" and "tier2" are REPLAYED from recorded data as-of the bar
        (real, not invented) but only for the window the recorder was running —
        outside it they return None; "tier1"/"setup" are always None in
        backtest. So ALWAYS handle None as "unknown", and use signals as a
        filter/confirmation, not the sole trigger, if you want the strategy to
        backtest cleanly over ranges predating the recording.

Act:   `ctx.enter(legs, tag="", sl_pct=None, target_pct=None) -> bool`
       (multi-leg atomic; returns False if paused / not enough capital;
        sl_pct/target_pct declare per-leg premium levels vs fill price —
        the engine enforces them and shows them on the dashboard),
       `ctx.set_levels(position_id, stop_loss=None, target=None)` to
       trail or adjust levels on an open position,
       `ctx.exit(position_id, reason="signal") -> bool`,
       `ctx.exit_all(reason="signal")`, `ctx.log(msg)`.
       Pass a specific `reason` (e.g. "time_exit") so the blotter records WHY
       you exited — it powers exit-attribution analysis. Leave "manual" alone;
       it means human intervention.

LegSpec fields: option_type (OptionType.CALL/PUT), action (Action.BUY/SELL),
strike_offset (int), expiry_kind (ExpiryKind.WEEKLY/MONTHLY),
expiry_offset (int, 0=nearest), lots (int), tag (str).

## Indicators toolbox (`indicators.*`, already available — do NOT import)

All take `bars` = a history window (`ctx.history(n)`, oldest first) and read
the tail; they return None when there isn't enough data, so guard on None.
Set `"warmup_bars"` in params (rule 8) so history is deep enough at bar 1.

Trend / momentum / volatility:
- `indicators.sma(bars, n, key="close")`, `indicators.ema(bars, n, key="close")`
- `indicators.rsi(bars, n=14)` → 0..100
- `indicators.macd(bars, fast=12, slow=26, signal=9)` → {macd, signal, hist}
- `indicators.atr(bars, n=14)`; `indicators.true_ranges(bars)` → list
- `indicators.bollinger(bars, n=20, k=2.0)` → {mid, upper, lower, width}
- `indicators.adx(bars, n=14)` → {adx, plus_di, minus_di}
- `indicators.supertrend(bars, period=10, mult=3.0)` → {dir: +1/-1, level}
- `indicators.vwap(bars)` → session-anchored (degrades to avg typical price
  on indexes, which carry no volume)

Price action / structure:
- `indicators.range_position(bar)` → 0 (at low) .. 1 (at high)
- `indicators.is_inside_bar(bars)` / `indicators.is_outside_bar(bars)` → bool
- `indicators.swing_high(bars, left=2, right=2)` / `swing_low(...)`
- `indicators.break_of_structure(bars, lookback=20)` → "up" / "down" / None

Session references / pivots (need multi-day history via warmup_bars):
- `indicators.prev_day(bars)` → {open, high, low, close} of the prior session
- `indicators.opening_range(bars, minutes=15)` → {high, low}
- `indicators.pivots(high, low, close)` → {p, r1, s1, r2, s2, r3, s3};
  `indicators.pivots_from_history(bars)` computes them from the prior session
- `indicators.cpr(high, low, close)` → {pivot, bc, tc, width}
- `indicators.gap_pct(bars)` → opening gap vs prior close, %

## Behavioral requirements
- Check `ctx.now.time()` for entry windows; the engine calls you on every bar.
- Guard against double entry: check `ctx.positions` / your own flags.
- Prefer declaring stops/targets via `sl_pct`/`target_pct` on entry (the
  engine enforces them even between your on_bar calls); add your own
  structure-level exits (combined premium, day P&L) inside `on_bar`.
- The engine force-squares-off expiring positions near close; still call
  `ctx.exit_all(reason="time_exit")` yourself when your logic says the day is
  done (use a descriptive reason so the exit is attributable later).

Now here is the strategy I want: <DESCRIBE YOUR STRATEGY HERE>
