# Example of what an LLM should produce with prompts/strategy_prompt.md.
# Paste this file's contents into POST /strategies to try the platform.

class ShortStraddle920(Strategy):
    """Sell ATM straddle at 09:20, 30% SL per leg, exit all at 15:10."""

    def __init__(self):
        self.params = {"entry_hour": 9, "entry_minute": 20,
                       "sl_pct": 0.30, "exit_hour": 15, "exit_minute": 10,
                       "lots": 1}
        self.entered_today = None  # date of last entry

    def meta(self) -> StrategyMeta:
        return StrategyMeta(
            name="Short Straddle 9:20",
            underlying="NIFTY",
            segment="NSE_FNO",
            timeframe="5",
            params=self.params,
            description="Intraday ATM short straddle with per-leg SL",
        )

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        t = ctx.now.time()
        today = ctx.now.date()

        # ---- exit window ------------------------------------------------
        if (t.hour, t.minute) >= (self.params["exit_hour"], self.params["exit_minute"]):
            if ctx.positions:
                ctx.log("time exit: squaring off")
                ctx.exit_all()
            return

        # ---- entry (SL declared to engine via sl_pct) ---------------------
        if self.entered_today == today or ctx.positions:
            return
        if (t.hour, t.minute) >= (self.params["entry_hour"], self.params["entry_minute"]):
            ok = ctx.enter([
                LegSpec(OptionType.CALL, Action.SELL, strike_offset=0,
                        expiry_kind=ExpiryKind.WEEKLY, lots=self.params["lots"],
                        tag="short_ce"),
                LegSpec(OptionType.PUT, Action.SELL, strike_offset=0,
                        expiry_kind=ExpiryKind.WEEKLY, lots=self.params["lots"],
                        tag="short_pe"),
            ], tag="straddle", sl_pct=self.params["sl_pct"])
            if ok:
                self.entered_today = today
                ctx.log(f"straddle sold, spot={ctx.spot:.1f}")

    def on_day_end(self, ctx: Context) -> None:
        if ctx.positions:
            ctx.exit_all()
