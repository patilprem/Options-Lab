# Range Income Seller — directional premium SELLER for choppy / slightly
# trending days. The regime gate is the whole edge: it only sells when ADX says
# there is NO strong trend and the previous day's CPR is not the narrow kind
# that precedes big trend days (narrow CPR -> trend day -> death for short
# options on the wrong side). Direction comes from structure: sell the side
# price is walking AWAY from (above VWAP + daily pivot -> sell the PUT below;
# below both -> sell the CALL above), so the short strike is backed by two
# levels the market would have to reclaim before it threatens the position.
# Paste this file's contents into New Strategy.

class RangeIncomeSeller(Strategy):
    """Sell one OTM weekly option against the day's structure, in chop only.

    Regime gate (skip the day entirely if it fails):
      * ADX < adx_max                    — choppy or only mildly trending
      * prev-day CPR width >= cpr_narrow_pct of spot — narrow CPR forecasts a
        trend day; missing prev-day data (day 1) never blocks

    Direction + quality (10:00-14:00, one trade per day):
      * above session VWAP AND above the classic daily pivot -> SELL PUT
        otm_offset strikes below ATM; below both -> SELL CALL above; mixed ->
        no trade (structure disagrees with itself)
      * optional (None never blocks): strong opposing index bias skips;
        IV skew veto (don't sell puts into heavy downside fear or calls into
        call-side fear); IV rank floor so the premium is worth selling

    Exits — collect decay, flee emerging trends:
      * engine-enforced sl_pct / target_pct on the premium
      * structure break: price closes back through BOTH VWAP and the pivot
        against the position -> exit (the lean was wrong)
      * trend emergence: ADX pushes above adx_max + 5 with Supertrend against
        the lean -> exit (the regime bet itself failed)
      * hard flat 15:08
    """

    def __init__(self):
        self.params = {
            "adx_period": 14, "adx_max": 27,
            "cpr_narrow_pct": 0.04,          # % of spot; narrower -> skip day
            "otm_offset": 2,                 # strikes away from ATM
            "sl_pct": 0.45, "target_pct": 0.55,
            "iv_min_rank": 40,               # only sell premium worth selling
            "skew_veto": 3.0,                # IV-pts of one-sided fear to veto
            "bias_gate": 0.4, "lots": 1,
            "st_period": 10, "st_mult": 3.0,
            "warmup_bars": 160,              # spans prev session (pivot/CPR)
        }
        self.traded_today = None

    def meta(self) -> StrategyMeta:
        return StrategyMeta(
            name="Range Income Seller",
            underlying="NIFTY", segment="NSE_FNO", timeframe="5",
            params=self.params,
            description="OTM premium seller gated to chop (ADX + CPR), "
                        "direction from VWAP + daily pivot structure",
        )

    # -- helpers --------------------------------------------------------------
    def _structure(self, ctx):
        """(vwap, pivot_p) for the latest session; either may be None."""
        bars = ctx.history(self.params["warmup_bars"] + 80)
        vw = indicators.vwap(bars)
        piv = indicators.pivots_from_history(bars)
        return vw, (piv["p"] if piv else None)

    def _regime_ok(self, ctx) -> bool:
        bars = ctx.history(self.params["warmup_bars"] + 5)
        adx = indicators.adx(bars, self.params["adx_period"])
        if adx is None or adx["adx"] >= self.params["adx_max"]:
            return False                     # unknown or trending -> don't sell
        pd = indicators.prev_day(bars)
        if pd is not None:
            c = indicators.cpr(pd["high"], pd["low"], pd["close"])
            spot = ctx.spot or 1.0
            if 100.0 * c["width"] / spot < self.params["cpr_narrow_pct"]:
                return False                 # narrow CPR -> likely trend day
        return True

    def _filters_ok(self, ctx, sell_put: bool) -> bool:
        b = ctx.signal("index_bias")
        if b and b.get("score") is not None and \
                abs(b["score"]) >= self.params["bias_gate"]:
            if (sell_put and b["score"] < 0) or (not sell_put and b["score"] > 0):
                return False                 # strong breadth against the lean
        c = ctx.chain()
        if c and c.get("iv_skew") is not None:
            if sell_put and c["iv_skew"] > self.params["skew_veto"]:
                return False                 # puts bid up hard: real fear, step aside
            if not sell_put and c["iv_skew"] < -self.params["skew_veto"]:
                return False
        r = ctx.iv_rank(30)
        if r is not None and r < self.params["iv_min_rank"]:
            return False                     # premium too cheap to be worth it
        return True

    def _manage(self, ctx):
        """Exit if structure breaks or a trend day starts developing."""
        if not ctx.positions:
            return
        vw, piv = self._structure(ctx)
        px = ctx.spot
        bars = ctx.history(self.params["warmup_bars"] + 5)
        adx = indicators.adx(bars, self.params["adx_period"])
        st = indicators.supertrend(bars, self.params["st_period"],
                                   self.params["st_mult"])
        for p in ctx.positions:
            short_put = p.leg.option_type == OptionType.PUT   # we only sell
            broken = (vw is not None and piv is not None and
                      ((short_put and px < vw and px < piv) or
                       (not short_put and px > vw and px > piv)))
            trending = (adx is not None and st is not None
                        and adx["adx"] > self.params["adx_max"] + 5
                        and ((short_put and st["dir"] == -1) or
                             (not short_put and st["dir"] == 1)))
            if broken or trending:
                ctx.log(("structure broken" if broken else "trend emerging")
                        + f" — exiting {p.tag or p.id}")
                ctx.exit(p.id, reason="signal")

    # -- main -----------------------------------------------------------------
    def on_bar(self, ctx: Context, bar: Bar) -> None:
        t = ctx.now.time()

        if (t.hour, t.minute) >= (15, 8):
            if ctx.positions:
                ctx.exit_all(reason="time_exit")
            return

        self._manage(ctx)

        today = ctx.now.date()
        if self.traded_today == today or ctx.positions:
            return
        if not ((10, 0) <= (t.hour, t.minute) <= (14, 0)):
            return
        if not self._regime_ok(ctx):
            return

        vw, piv = self._structure(ctx)
        if vw is None or piv is None:
            return                           # no structure read -> no lean
        px = bar.close
        if px > vw and px > piv:
            sell_put = True                  # drifting up -> sell the downside
        elif px < vw and px < piv:
            sell_put = False                 # drifting down -> sell the upside
        else:
            return                           # VWAP and pivot disagree -> chop noise
        if not self._filters_ok(ctx, sell_put):
            ctx.log(f"{'PE' if sell_put else 'CE'} sell skipped by bias/skew/IV filter")
            return

        off = -self.params["otm_offset"] if sell_put else self.params["otm_offset"]
        otype = OptionType.PUT if sell_put else OptionType.CALL
        ok = ctx.enter(
            [LegSpec(otype, Action.SELL, strike_offset=off,
                     expiry_kind=ExpiryKind.WEEKLY, lots=self.params["lots"],
                     tag="income")],
            tag="income", sl_pct=self.params["sl_pct"],
            target_pct=self.params["target_pct"])
        if ok:
            self.traded_today = today
            ctx.log(f"sold {'PE' if sell_put else 'CE'}{off:+d} @ spot {px:.1f} "
                    f"(vwap {vw:.1f}, pivot {piv:.1f})")

    def on_day_end(self, ctx: Context) -> None:
        if ctx.positions:
            ctx.exit_all(reason="time_exit")
