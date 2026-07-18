# Trend Rider v2 — momentum option BUYER (slightly ITM) built to catch BIG
# trend days. v2 is tuned from the first real 2-year backtest's attribution
# (2024-07 -> 2026-07, +21.9%, Sharpe 1.2), which exposed two drags:
#   * the Supertrend-flip exit fired on single-bar noise: 151 flip exits at
#     29.8% win rate cost -1.1L, while the 53 trades that survived to the
#     time exit made +1.86L -> the flip now needs `flip_confirm_bars`
#     CONSECUTIVE flipped bars before it cuts the trade.
#   * dead-IV entries washed out: 115 trades below IV-rank 30 netted ~zero
#     while paying fees -> new `iv_rank_floor` skips them (cheap IV = dead
#     tape = breakouts are usually fakeouts).
# Paste this file's contents into New Strategy.

class TrendRiderITM(Strategy):
    """Buy a slightly ITM weekly option WITH a confirmed multi-timeframe trend.

    Entry (bullish; mirror for bearish), 09:30-14:15, up to 2 entries/day:
      * 60-min close above its EMA20        — the higher timeframe agrees
      * 5-min Supertrend UP and close above EMA20
      * ADX >= adx_min                      — real momentum, not drift
      * close breaks the prior `breakout_bars` high by breakout_atr * ATR
      * IV rank inside [iv_rank_floor, iv_rank_cap] when known — skip dead
        tape (nothing expands) AND already-rich premium (overpaying);
        unknown (None) never blocks, so it stays fully backtestable
      * optional: index bias not strongly against
      -> BUY CALL strike_offset=-1 (PUT +1 for the bearish mirror)

    Exits — built to CATCH the big one, not scalp it:
      * engine-enforced disaster stop sl_pct below entry premium
      * once premium is +arm_pct, trail a stop trail_pct below the premium
        high (ratchets up only; locks breakeven first)
      * Supertrend flipped against the position for `flip_confirm_bars`
        CONSECUTIVE bars -> exit (a one-bar whip no longer cuts the ride)
      * hard flat 15:12
    """

    def __init__(self):
        self.params = {
            "ema_period": 20, "htf_interval": 60, "htf_ema": 20,
            "st_period": 10, "st_mult": 3.0,
            "adx_period": 14, "adx_min": 22,
            "atr_period": 14, "breakout_bars": 10, "breakout_atr": 0.3,
            "itm_offset": 1,                 # strikes IN the money (1 = slight)
            "sl_pct": 0.35,                  # disaster stop on premium
            "arm_pct": 0.25,                 # gain that activates the trail
            "trail_pct": 0.20,               # trail distance below premium high
            "flip_confirm_bars": 2,          # v2: bars a flip must persist to exit
            "bias_gate": 0.3,
            "iv_rank_floor": 30,             # v2: skip dead-IV tape (fakeout zone)
            "iv_rank_cap": 75,               # don't overpay for rich premium
            "max_entries_per_day": 2, "lots": 1,
            "warmup_bars": 90,               # settled EMA/ADX/Supertrend + HTF
        }
        self.day = None
        self.entries_today = 0
        self.prem_high = {}                  # position id -> premium high-water
        self.flip_streak = 0                 # consecutive bars Supertrend is against us

    def meta(self) -> StrategyMeta:
        return StrategyMeta(
            name="Trend Rider ITM v2",
            underlying="NIFTY", segment="NSE_FNO", timeframe="5",
            params=self.params,
            description="Slightly-ITM option buyer: MTF trend + ADX + ATR "
                        "breakout entry, confirmed-flip + trailing-stop exits "
                        "to ride big trends (v2: flip confirmation, IV floor)",
        )

    # -- helpers --------------------------------------------------------------
    def _roll_day(self, ctx):
        d = ctx.now.date()
        if d != self.day:
            self.day = d
            self.entries_today = 0
            self.prem_high = {}
            self.flip_streak = 0

    def _bias_ok(self, ctx, want_ce: bool) -> bool:
        """Veto only on a STRONG opposing breadth read; unknown never blocks."""
        b = ctx.signal("index_bias")
        if not b or b.get("score") is None:
            return True
        s = b["score"]
        if abs(s) < self.params["bias_gate"]:
            return True
        return s > 0 if want_ce else s < 0

    def _manage(self, ctx):
        """Trail winners; exit on a CONFIRMED trend flip. Runs before entries."""
        if not ctx.positions:
            self.flip_streak = 0
            return
        st = indicators.supertrend(ctx.history(self.params["st_period"] * 4 + 5),
                                   self.params["st_period"], self.params["st_mult"])
        for p in ctx.positions:
            # trend flipped against the long option — but only a flip that
            # HOLDS for flip_confirm_bars consecutive bars ends the ride
            # (v2: single-bar flips exited 151 trades at 29.8% WR for -1.1L
            # while trades left to run made +1.86L)
            long_ce = p.leg.option_type == OptionType.CALL
            flipped = st is not None and (
                (long_ce and st["dir"] == -1) or (not long_ce and st["dir"] == 1))
            if flipped:
                self.flip_streak += 1
                if self.flip_streak >= self.params["flip_confirm_bars"]:
                    ctx.log(f"confirmed trend flip ({self.flip_streak} bars) — "
                            f"exiting {p.tag or p.id}")
                    ctx.exit(p.id, reason="signal")
                    self.flip_streak = 0
                    continue
            else:
                self.flip_streak = 0
            # ratcheting premium trail (up only; arms after +arm_pct)
            high = max(self.prem_high.get(p.id, p.entry_price), p.mtm_price)
            self.prem_high[p.id] = high
            if high >= p.entry_price * (1 + self.params["arm_pct"]):
                new_stop = max(high * (1 - self.params["trail_pct"]),
                               p.entry_price)          # never worse than breakeven
                if p.stop_loss is None or new_stop > p.stop_loss:
                    ctx.set_levels(p.id, stop_loss=round(new_stop, 2))

    # -- main -----------------------------------------------------------------
    def on_bar(self, ctx: Context, bar: Bar) -> None:
        self._roll_day(ctx)
        t = ctx.now.time()

        if (t.hour, t.minute) >= (15, 12):
            if ctx.positions:
                ctx.exit_all(reason="time_exit")
            return

        self._manage(ctx)

        # entry gate
        if ctx.positions or self.entries_today >= self.params["max_entries_per_day"]:
            return
        if not ((9, 30) <= (t.hour, t.minute) <= (14, 15)):
            return

        n = self.params["warmup_bars"]
        bars = ctx.history(n + 5)
        ema = indicators.ema(bars, self.params["ema_period"])
        st = indicators.supertrend(bars, self.params["st_period"], self.params["st_mult"])
        adx = indicators.adx(bars, self.params["adx_period"])
        a = indicators.atr(bars, self.params["atr_period"])
        htf = ctx.history(self.params["htf_ema"] + 5,
                          interval=self.params["htf_interval"])
        htf_ema = indicators.ema(htf, self.params["htf_ema"])
        if None in (ema, a) or st is None or adx is None:
            return                                   # not enough history yet
        if adx["adx"] < self.params["adx_min"]:
            return                                   # no momentum -> no chase

        nb = self.params["breakout_bars"]
        if len(bars) < nb + 2:
            return
        prior_high = max(b.high for b in bars[-(nb + 1):-1])
        prior_low = min(b.low for b in bars[-(nb + 1):-1])
        buf = self.params["breakout_atr"] * a
        px = bar.close
        htf_up = htf_ema is None or (htf and htf[-1].close > htf_ema)
        htf_dn = htf_ema is None or (htf and htf[-1].close < htf_ema)

        want_ce = (px > ema and st["dir"] == 1 and htf_up
                   and px > prior_high + buf)
        want_pe = (px < ema and st["dir"] == -1 and htf_dn
                   and px < prior_low - buf)
        if not (want_ce or want_pe):
            return
        if not self._bias_ok(ctx, want_ce):
            ctx.log(f"breakout {'CE' if want_ce else 'PE'} skipped: bias against")
            return
        r = ctx.iv_rank(30)
        if r is not None:
            if r > self.params["iv_rank_cap"]:
                ctx.log(f"entry skipped: IV rank {r:.0f} too rich to buy")
                return
            if r < self.params["iv_rank_floor"]:
                # v2: 115 sub-30 trades netted ~zero — dead IV means dead tape
                ctx.log(f"entry skipped: IV rank {r:.0f} too dead to buy")
                return

        # slightly ITM: CALL below ATM / PUT above ATM
        off = -self.params["itm_offset"] if want_ce else self.params["itm_offset"]
        otype = OptionType.CALL if want_ce else OptionType.PUT
        ok = ctx.enter(
            [LegSpec(otype, Action.BUY, strike_offset=off,
                     expiry_kind=ExpiryKind.WEEKLY, lots=self.params["lots"],
                     tag="rider")],
            tag="rider", sl_pct=self.params["sl_pct"])   # NO target: let it run
        if ok:
            self.entries_today += 1
            self.flip_streak = 0
            ctx.log(f"trend entry {'CE' if want_ce else 'PE'}{off:+d} @ spot "
                    f"{px:.1f} (adx {adx['adx']:.0f}, atr {a:.1f})")

    def on_day_end(self, ctx: Context) -> None:
        if ctx.positions:
            ctx.exit_all(reason="time_exit")
