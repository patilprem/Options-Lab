# Iron Condor Harvest — LOW-RISK premium seller for choppy / low-trend days.
# Replaces the retired Range Income Seller with a STRUCTURAL fix rather than
# another round of filter tuning. What killed the seller (2-year backtest:
# -14.5%, Sharpe -3.64) was naked short risk: wrong days cost ~4x right days
# (-2,442 avg stop-loss vs +391/610 avg winners) and a premium stop checked
# once per closed 5-min bar could not reliably cap an intra-bar run. A condor
# fixes that at the position level:
#   * SELL an OTM call AND an OTM put (delta-neutral: no directional lean to
#     be wrong about), BUY further-OTM wings on both sides.
#   * Max loss = wing width - credit received, capped BY THE STRUCTURE — a
#     trend day costs a known, small, pre-computed amount even if every stop
#     misses. On NIFTY with 2-strike wings that is roughly (100 pts x lot)
#     minus credit ~= Rs.4-5k per lot: genuinely low risk per trade.
#   * Profit = time decay while the index stays inside the short strikes.
# Regime gates kept deliberately FEW (the seller's post-mortem showed gate
# tuning wasn't the fix): a range-expansion veto and a drift veto (both
# forward-looking, from price itself), plus a coarse ADX ceiling — the capped
# structure tolerates the mild-trend days that leak through.
# Paste this file's contents into New Strategy.

class IronCondorHarvest(Strategy):
    """Intraday NIFTY iron condor for rangebound days.

    Entry (10:15-13:00, one condor per day) — all gates must pass:
      * today's range so far < range_veto_ratio * yesterday's full range
        (range already expanding = trend day forming = no condor)
      * |spot - today's open| < drift_veto_ratio * yesterday's range
        (steady one-way drift = directional day = no condor)
      * ADX < adx_max (coarse; the wings make mild trends survivable)
      * IV rank >= iv_min_rank when known (premium worth selling;
        None never blocks, so it stays fully backtestable)
      * credit >= min_credit_ratio * wing width (skip when the structure
        pays too little for the risk — fees would eat it)
      -> SELL CE +short_offset / PE -short_offset,
         BUY  CE +(short_offset+wing_strikes) / PE -(short_offset+wing_strikes)

    Exits (structure-level; no per-leg stops — the wings ARE the safety net):
      * keep target_frac of the credit -> buy the condor back (target)
      * combined P&L worse than -loss_mult x credit -> out early (signal)
      * spot crosses EITHER short strike -> the range thesis is dead (signal)
      * hard flat 15:05 (the engine also force-squares-off expiring legs)
    """

    def __init__(self):
        self.params = {
            "short_offset": 3,               # strikes OTM for the short legs
            "wing_strikes": 2,               # wings this many strikes further out
            "adx_period": 14, "adx_max": 25,
            "range_veto_ratio": 0.6,         # today's range vs yesterday's
            "drift_veto_ratio": 0.35,        # |spot - open| vs yesterday's range
            "min_credit_ratio": 0.15,        # credit must be >= this x wing width
            "target_frac": 0.6,              # exit keeping 60% of the credit
            "loss_mult": 1.0,                # early out at -1x credit (wings cap worse)
            "iv_min_rank": 25,
            "lots": 1,
            "warmup_bars": 160,              # spans prev session (range/drift gates)
        }
        self.traded_today = None
        self.skip_logged = None              # throttle thin-credit log to 1/day

    def meta(self) -> StrategyMeta:
        return StrategyMeta(
            name="Iron Condor Harvest",
            underlying="NIFTY", segment="NSE_FNO", timeframe="5",
            params=self.params,
            description="Delta-neutral intraday iron condor for chop: risk "
                        "capped by the wings, range/drift vetoes at entry, "
                        "structure-level credit management",
        )

    # -- helpers --------------------------------------------------------------
    def _regime_ok(self, ctx) -> bool:
        bars = ctx.history(self.params["warmup_bars"] + 5)
        adx = indicators.adx(bars, self.params["adx_period"])
        if adx is None or adx["adx"] >= self.params["adx_max"]:
            return False                     # unknown or trending -> no condor
        pd = indicators.prev_day(bars)
        today = ctx.now.date()
        tbars = [b for b in bars if b.ts.date() == today]
        if pd is None or not tbars:
            return False                     # need yesterday's range to judge today
        prev_range = pd["high"] - pd["low"]
        if prev_range <= 0:
            return False
        trange = max(b.high for b in tbars) - min(b.low for b in tbars)
        if trange > self.params["range_veto_ratio"] * prev_range:
            return False                     # range expanding -> trend day forming
        drift = abs(ctx.spot - tbars[0].open)
        if drift > self.params["drift_veto_ratio"] * prev_range:
            return False                     # one-way drift -> directional day
        r = ctx.iv_rank(30)
        if r is not None and r < self.params["iv_min_rank"]:
            return False                     # premium too cheap to be worth it
        return True

    def _condor_legs(self):
        so, w = self.params["short_offset"], self.params["wing_strikes"]
        lots = self.params["lots"]
        return [
            LegSpec(OptionType.CALL, Action.SELL, strike_offset=so,
                    expiry_kind=ExpiryKind.WEEKLY, lots=lots, tag="sc"),
            LegSpec(OptionType.CALL, Action.BUY, strike_offset=so + w,
                    expiry_kind=ExpiryKind.WEEKLY, lots=lots, tag="lc"),
            LegSpec(OptionType.PUT, Action.SELL, strike_offset=-so,
                    expiry_kind=ExpiryKind.WEEKLY, lots=lots, tag="sp"),
            LegSpec(OptionType.PUT, Action.BUY, strike_offset=-(so + w),
                    expiry_kind=ExpiryKind.WEEKLY, lots=lots, tag="lp"),
        ]

    def _manage(self, ctx):
        """Structure-level management: the four legs live and die together."""
        if not ctx.positions:
            return
        shorts = [p for p in ctx.positions if p.qty < 0]
        longs = [p for p in ctx.positions if p.qty > 0]
        credit_rs = (sum(p.entry_price * abs(p.qty) for p in shorts)
                     - sum(p.entry_price * abs(p.qty) for p in longs))
        unreal = sum(p.unrealized_pnl for p in ctx.positions)
        # 1) decay target: most of the credit is banked -> take it
        if credit_rs > 0 and unreal >= self.params["target_frac"] * credit_rs:
            ctx.log(f"condor target: banked {unreal:,.0f} of {credit_rs:,.0f} credit")
            ctx.exit_all(reason="target")
            return
        # 2) early loss cut: give back the credit once, not the whole width
        #    (the wings still cap the true worst case if this misses intra-bar)
        if credit_rs > 0 and unreal <= -self.params["loss_mult"] * credit_rs:
            ctx.log(f"condor loss cut at {unreal:,.0f} (credit {credit_rs:,.0f})")
            ctx.exit_all(reason="signal")
            return
        # 3) short strike breached: the range thesis is dead, don't hope
        px = ctx.spot
        for p in shorts:
            ce = p.leg.option_type == OptionType.CALL
            if (ce and px > p.strike) or (not ce and px < p.strike):
                ctx.log(f"short strike {p.strike:g} breached @ {px:.1f} — closing condor")
                ctx.exit_all(reason="signal")
                return

    # -- main -----------------------------------------------------------------
    def on_bar(self, ctx: Context, bar: Bar) -> None:
        t = ctx.now.time()

        if (t.hour, t.minute) >= (15, 5):
            if ctx.positions:
                ctx.exit_all(reason="time_exit")
            return

        self._manage(ctx)

        today = ctx.now.date()
        if self.traded_today == today or ctx.positions:
            return
        if not ((10, 15) <= (t.hour, t.minute) <= (13, 0)):
            return
        if not self._regime_ok(ctx):
            return

        # preview the four quotes: is the credit worth the capped risk?
        legs = self._condor_legs()
        qs = [ctx.option(l) for l in legs]
        if any(q is None for q in qs):
            # LOUD, not silent: a 2-year backtest once produced exactly ONE
            # condor because the store's option history only covered ATM+/-2
            # while the legs need +/-(short_offset+wing_strikes) — and this
            # branch skipped every day without a word. If you see this log,
            # re-backfill with Strikes ATM+/-5 on the Data tab.
            if self.skip_logged != today:
                self.skip_logged = today
                missing = [f"{l.option_type.value[0]}{l.strike_offset:+d}"
                           for l, q in zip(legs, qs) if q is None]
                ctx.log(f"condor skipped: no quote for {', '.join(missing)} "
                        "(option history missing these strikes?)")
            return
        sc, lc, sp, lp = qs
        credit = (sc.ltp + sp.ltp) - (lc.ltp + lp.ltp)
        width = lc.strike - sc.strike        # rupee width of one wing
        if width <= 0 or credit < self.params["min_credit_ratio"] * width:
            if self.skip_logged != today:    # once a day, not every bar
                self.skip_logged = today
                ctx.log(f"condor skipped: credit {credit:.1f} too thin vs width {width:g}")
            return

        # NO per-leg sl/target: the long wings cap the worst case structurally,
        # and management above works on the combined credit, not leg premiums.
        ok = ctx.enter(legs, tag="condor")
        if ok:
            self.traded_today = today
            ctx.log(f"condor sold: credit {credit:.1f} pts, wings {width:g} wide, "
                    f"shorts {sp.strike:g}P/{sc.strike:g}C @ spot {ctx.spot:.1f}")

    def on_day_end(self, ctx: Context) -> None:
        if ctx.positions:
            ctx.exit_all(reason="time_exit")
