# Example: a price-action option-BUYER built entirely on the `indicators`
# toolbox (no hand-rolled math) and declaring warmup_bars so its indicators
# are settled from the first bar. Buys an ATM option in the direction of an
# established intraday trend, confirmed by EMA slope, Supertrend, VWAP and a
# real ATR-scaled breakout. Paste this file's contents into New Strategy.
#
# Contrast with pivot_confluence_seller.py, which hand-rolls Supertrend/EMA/
# VWAP with manual warmup counters — this is the same idea, a fraction of the
# code, and the indicator formulas are unit-tested platform code.

class EmaAtrTrend(Strategy):
    """Long an ATM weekly option WITH the intraday trend.

    Enter (mirror for shorts): close above session VWAP AND above EMA20 AND
    Supertrend UP AND price breaking the last 5-bar high by > 0.25*ATR ->
    BUY an ATM CALL. Bearish mirror -> BUY an ATM PUT. One trade per day,
    10:00-14:00, engine-enforced stop/target on the premium; hard flat 15:10.
    """

    def __init__(self):
        self.params = {
            "ema_period": 20, "st_period": 10, "st_mult": 3.0, "atr_period": 14,
            "breakout_atr": 0.25, "sl_pct": 0.35, "target_pct": 0.70, "lots": 1,
            "htf_interval": 60,      # higher-timeframe trend filter
            "htf_ema": 20,
            "iv_rank_cap": 70,       # don't buy options when IV is already rich
            # deep enough for a settled EMA20/Supertrend/ATR at bar 1
            "warmup_bars": 60,
        }
        self.entered_today = None

    def _htf_trend_ok(self, ctx, want_ce: bool) -> bool:
        """Higher-timeframe agreement via ctx.history(interval=). Unknown
        (no data) -> don't block, so the strategy stays backtestable."""
        htf = ctx.history(self.params["htf_ema"] + 5, interval=self.params["htf_interval"])
        e = indicators.ema(htf, self.params["htf_ema"])
        if e is None:
            return True
        return htf[-1].close > e if want_ce else htf[-1].close < e

    def _iv_not_rich(self, ctx) -> bool:
        """Skip buying when ATM IV is in the top of its range. None -> allow."""
        r = ctx.iv_rank(30)
        return r is None or r <= self.params["iv_rank_cap"]

    def meta(self) -> StrategyMeta:
        return StrategyMeta(
            name="EMA/ATR Trend Buyer",
            underlying="NIFTY", segment="NSE_FNO", timeframe="5",
            params=self.params,
            description="ATM option buy with the intraday trend (EMA/Supertrend/VWAP/ATR)",
        )

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        t = ctx.now.time()
        today = ctx.now.date()

        # hard flat late in the session
        if (t.hour, t.minute) >= (15, 10):
            if ctx.positions:
                ctx.exit_all(reason="time_exit")
            return

        # one trade per day, only in the 10:00-14:00 window
        if self.entered_today == today or ctx.positions:
            return
        if not ((10, 0) <= (t.hour, t.minute) <= (14, 0)):
            return

        bars = ctx.history(self.params["warmup_bars"] + 5)
        ema = indicators.ema(bars, self.params["ema_period"])
        st = indicators.supertrend(bars, self.params["st_period"], self.params["st_mult"])
        vw = indicators.vwap(bars)
        a = indicators.atr(bars, self.params["atr_period"])
        # any missing read -> not enough history yet, wait
        if None in (ema, vw, a) or st is None or len(bars) < 6:
            return

        prior_high = max(b.high for b in bars[-6:-1])
        prior_low = min(b.low for b in bars[-6:-1])
        buf = self.params["breakout_atr"] * a
        px = bar.close

        want_ce = (px > vw and px > ema and st["dir"] == 1
                   and px > prior_high + buf)
        want_pe = (px < vw and px < ema and st["dir"] == -1
                   and px < prior_low - buf)
        if not (want_ce or want_pe):
            return

        # optional confirmations (skip when data is unknown -> backtestable):
        # higher-timeframe trend agreement + don't buy already-rich IV
        if not self._htf_trend_ok(ctx, want_ce) or not self._iv_not_rich(ctx):
            ctx.log(f"{'CE' if want_ce else 'PE'} setup skipped: HTF/IV filter")
            return

        otype = OptionType.CALL if want_ce else OptionType.PUT
        ok = ctx.enter(
            [LegSpec(otype, Action.BUY, strike_offset=0,
                     expiry_kind=ExpiryKind.WEEKLY, lots=self.params["lots"],
                     tag="trend")],
            tag="trend", sl_pct=self.params["sl_pct"],
            target_pct=self.params["target_pct"])
        if ok:
            self.entered_today = today
            ctx.log(f"trend {'CE' if want_ce else 'PE'} entry @ {px:.1f} "
                    f"(vwap {vw:.1f}, ema {ema:.1f}, atr {a:.1f})")
