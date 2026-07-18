# Strategy Generation Prompt (paste this into your LLM, then describe your strategy)

You are writing a trading strategy for my options platform "OptionsLab".
Output ONLY Python code, no explanations, no markdown fences.

## Hard rules
1. Define EXACTLY ONE class that subclasses `Strategy`.
2. Do NOT write any import statements. These names are already available:
   `Strategy, Context, StrategyMeta, LegSpec, Bar, OptionQuote, Position,
   OptionType, Action, ExpiryKind`. (If you truly need it, only `math`,
   `statistics` and `datetime` may be imported — nothing else.)
3. Never touch files, network, threads, `eval`, `exec`, or `open`.
4. All market access and orders go through the `ctx` object ONLY.
5. Strikes are RELATIVE: `strike_offset=0` is ATM, `+2` is two strikes
   above ATM, `-1` is one below. Never use absolute strike numbers.
6. Expose every tunable number in `meta().params` and read from
   `self.params` so I can tweak them without regenerating code.
7. Keep per-bar work light: no heavy loops over long history each bar.
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
