# Example: an index option-BUYING strategy that uses the live FNO-scanner
# bias (F6) as an OPTIONAL filter. Demonstrates the honest ctx.signal() usage:
# the signal is live-only (None in backtests), so the trade logic still stands
# on its own price trigger and merely *skips* when the breadth bias disagrees.
# Paste this file's contents into POST /strategies to try it.

class IndexMomentumWithBias(Strategy):
    """Buy an ATM option on an opening-range breakout, but only when the FNO
    breadth bias agrees with the direction. Fully backtestable: with no scanner
    (backtest), the bias filter is simply skipped."""

    def __init__(self):
        self.params = {"range_min": 15, "sl_pct": 0.30, "target_pct": 0.60,
                       "exit_hour": 14, "exit_minute": 45, "lots": 1,
                       "bias_gate": 0.3}   # min |bias| to require agreement
        self.hi = self.lo = None
        self.entered_today = None

    def meta(self) -> StrategyMeta:
        return StrategyMeta(
            name="Index Momentum + Bias",
            underlying="NIFTY",
            segment="NSE_FNO",
            timeframe="5",
            params=self.params,
            description="ORB ATM option buy, filtered by live FNO breadth bias",
        )

    def _bias_ok(self, ctx, want_ce: bool) -> bool:
        """True if the live bias agrees (or is unavailable/weak -> don't block).
        A backtest returns None here, so the strategy stays backtestable."""
        b = ctx.signal("index_bias")
        if not b or b.get("score") is None:
            return True                        # unknown -> don't veto
        score = b["score"]
        if abs(score) < self.params["bias_gate"]:
            return True                        # no strong bias -> allow
        return score > 0 if want_ce else score < 0

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        t = ctx.now.time()
        today = ctx.now.date()

        # reset the opening range at the start of each day
        if self.entered_today != today and (t.hour, t.minute) <= (9, 15):
            self.hi = self.lo = None

        # exit window
        if (t.hour, t.minute) >= (self.params["exit_hour"], self.params["exit_minute"]):
            if ctx.positions:
                ctx.exit_all()
            return

        # build the opening range for the first `range_min` minutes
        open_end = (9, 15 + self.params["range_min"])
        if (t.hour, t.minute) < open_end:
            self.hi = bar.high if self.hi is None else max(self.hi, bar.high)
            self.lo = bar.low if self.lo is None else min(self.lo, bar.low)
            return

        if self.entered_today == today or ctx.positions or self.hi is None:
            return

        # breakout trigger + bias filter
        want_ce = bar.close > self.hi
        want_pe = bar.close < self.lo
        if not (want_ce or want_pe):
            return
        if not self._bias_ok(ctx, want_ce):
            ctx.log(f"breakout {'CE' if want_ce else 'PE'} skipped: bias disagrees")
            return

        leg = LegSpec(
            OptionType.CALL if want_ce else OptionType.PUT, Action.BUY,
            strike_offset=0, expiry_kind=ExpiryKind.WEEKLY,
            lots=self.params["lots"], tag="momo")
        ok = ctx.enter([leg], tag="orb_buy",
                       sl_pct=self.params["sl_pct"],
                       target_pct=self.params["target_pct"])
        if ok:
            self.entered_today = today
            ctx.log(f"bought {'CE' if want_ce else 'PE'} on breakout, spot={ctx.spot:.1f}")

    def on_day_end(self, ctx: Context) -> None:
        if ctx.positions:
            ctx.exit_all()
