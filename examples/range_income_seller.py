# Range Income Seller v2 — directional premium SELLER for choppy / slightly
# trending days. v2 is rebuilt from the first real 2-year backtest (-14.5%,
# Sharpe -3.64), whose exit-reason table showed the exact failure shape:
# calm-day trades worked (time_exit 73% WR +391 avg, target 100% WR +610 avg)
# but trend-day trades lost 4x as much per trade (stop_loss -2,442 avg,
# late defensive exits -1,078 avg at 12% WR). Every bucket of IV rank lost,
# so richer premium was NOT the fix — the regime gate letting trend days
# through was. v2 therefore:
#   * cuts the loss unit: sl_pct 0.45 -> 0.30 (also headroom for the fact
#     that stops are only checked once per closed 5-min bar)
#   * makes the regime gate forward-looking, not just level-based: ADX must
#     be low (23) AND not RISING (rising ADX = trend forming under our feet),
#     AND today's range so far must not already exceed `day_range_ratio` of
#     the previous day's range (range expansion in progress = trend day)
#   * exits failed days EARLIER: one structure level (VWAP or pivot) crossed
#     while the premium is soft_stop_pct against us -> out at ~-15% instead
#     of waiting for both levels (old rule) or the -30/-45% hard stop
# Paste this file's contents into New Strategy.

class RangeIncomeSeller(Strategy):
    """Sell one OTM weekly option against the day's structure, in chop only.

    Regime gate (skip the day entirely if ANY fails):
      * ADX < adx_max                  — choppy or only mildly trending
      * ADX not rising: now vs `adx_rise_lookback` bars ago must be under
        +adx_rise_pts (a lagging indicator's level can look calm while its
        SLOPE is screaming trend — the slope veto is the forward-looking half)
      * today's range so far < day_range_ratio * previous day's range
        (range already expanding -> trend day forming -> no selling)
      * prev-day CPR width >= cpr_narrow_pct of spot (narrow CPR forecasts a
        trend day); missing prev-day data (day 1) never blocks

    Direction + quality (10:00-14:00, one trade per day):
      * above session VWAP AND above the classic daily pivot -> SELL PUT
        otm_offset strikes below ATM; below both -> SELL CALL above; mixed ->
        no trade (structure disagrees with itself)
      * optional (None never blocks): strong opposing index bias skips;
        IV skew veto; IV rank floor so the premium is worth selling

    Exits — collect decay, flee trend days EARLY:
      * engine-enforced sl_pct / target_pct on the premium
      * SOFT structure break: EITHER level (VWAP or pivot) crossed against
        the lean while premium is >= soft_stop_pct against us -> exit early
      * HARD structure break: BOTH levels crossed -> exit regardless of P&L
      * trend emergence: ADX pushes above adx_max + 5 with Supertrend against
        the lean -> exit (the regime bet itself failed)
      * hard flat 15:08
    """

    def __init__(self):
        self.params = {
            "adx_period": 14, "adx_max": 23,
            "adx_rise_pts": 2.0, "adx_rise_lookback": 6,   # v2: slope veto
            "day_range_ratio": 0.75,                        # v2: expansion veto
            "cpr_narrow_pct": 0.04,          # % of spot; narrower -> skip day
            "otm_offset": 2,                 # strikes away from ATM
            "sl_pct": 0.30, "target_pct": 0.55,
            "soft_stop_pct": 0.15,           # v2: early exit on cracked structure
            "iv_min_rank": 40,               # only sell premium worth selling
            "skew_veto": 3.0,                # IV-pts of one-sided fear to veto
            "bias_gate": 0.4, "lots": 1,
            "st_period": 10, "st_mult": 3.0,
            "warmup_bars": 160,              # spans prev session (pivot/CPR)
        }
        self.traded_today = None

    def meta(self) -> StrategyMeta:
        return StrategyMeta(
            name="Range Income Seller v2",
            underlying="NIFTY", segment="NSE_FNO", timeframe="5",
            params=self.params,
            description="OTM premium seller gated to chop (ADX level+slope, "
                        "range expansion, CPR), VWAP+pivot direction, early "
                        "soft-stop exit (v2: tighter risk, forward regime gate)",
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
        n = self.params["adx_period"]
        adx = indicators.adx(bars, n)
        if adx is None or adx["adx"] >= self.params["adx_max"]:
            return False                     # unknown or trending -> don't sell
        # v2 slope veto: a calm LEVEL with a rising slope is a trend being
        # born — exactly the day the old gate kept selling into.
        look = self.params["adx_rise_lookback"]
        if len(bars) > look + 2 * n + 1:
            prev = indicators.adx(bars[:-look], n)
            if prev is not None and adx["adx"] > prev["adx"] + self.params["adx_rise_pts"]:
                return False
        pd = indicators.prev_day(bars)
        today = ctx.now.date()
        tbars = [b for b in bars if b.ts.date() == today]
        if pd is not None:
            c = indicators.cpr(pd["high"], pd["low"], pd["close"])
            spot = ctx.spot or 1.0
            if 100.0 * c["width"] / spot < self.params["cpr_narrow_pct"]:
                return False                 # narrow CPR -> likely trend day
            # v2 expansion veto: if today has ALREADY covered most of
            # yesterday's whole range by entry time, the range is expanding —
            # that IS a trend day, whatever ADX still says.
            prev_range = pd["high"] - pd["low"]
            if tbars and prev_range > 0:
                trange = max(b.high for b in tbars) - min(b.low for b in tbars)
                if trange > self.params["day_range_ratio"] * prev_range:
                    return False
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
        """Exit if structure cracks (early, P&L-aware) or breaks (hard), or a
        trend day starts developing. v2: the old rule waited for BOTH levels
        to break with no P&L awareness — those exits averaged -1,078 at 12%
        WR, i.e. they fired only after the damage was done."""
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
            if short_put:
                vw_x = vw is not None and px < vw
                piv_x = piv is not None and px < piv
            else:
                vw_x = vw is not None and px > vw
                piv_x = piv is not None and px > piv
            hard_break = vw_x and piv_x
            # soft break: one level gone AND the premium already moving
            # against us — cut at ~soft_stop_pct instead of riding to the stop
            under_water = p.mtm_price >= p.entry_price * (1 + self.params["soft_stop_pct"])
            soft_break = (vw_x or piv_x) and under_water
            trending = (adx is not None and st is not None
                        and adx["adx"] > self.params["adx_max"] + 5
                        and ((short_put and st["dir"] == -1) or
                             (not short_put and st["dir"] == 1)))
            if hard_break or soft_break or trending:
                why = ("structure broken" if hard_break else
                       "structure cracked + under water" if soft_break else
                       "trend emerging")
                ctx.log(f"{why} — exiting {p.tag or p.id}")
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
