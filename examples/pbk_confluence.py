# PBK Confluence — pullback-continuation option buyer (NIFTY, 5-min).
# The only archetype that stayed OOS-positive in the 2024-26 research pass
# (IS +5.8 pts/trade -> OOS +2.2, ~52% win at 50/50). Paste into New Strategy.

class PBKConfluence(Strategy):
    """Buy the dip in an established intraday trend.

    Long entry (short is the mirror), all must hold on one 5-min bar:
      * EMA9 above EMA21 for the last 6 bars (trend established ~30 min)
      * price touched EMA9 within the last 3 bars (the pullback)
      * this bar closes above the previous bar's high (resumption)
      * this bar's range >= 1.2 x ATR20 (conviction, not drift)
      * time between 10:00 and 14:30 (skip open/close noise)
    Exit: spot moves +target/-stop points from entry, else hard flat 15:00.
    One trade per day, 1 lot, ATM weekly. Premium SL declared as safety net.
    """

    def __init__(self):
        self.params = {"target_pts": 50, "stop_pts": 50, "atr_mult": 1.2,
                       "sl_pct": 0.45, "lots": 1}
        self.e9 = None
        self.e21 = None
        self.atr = None
        self.prev_close = None
        self.prev_bar = None
        self.align = 0          # consecutive bars of EMA alignment (+up / -down)
        self.touch_up = 99      # bars since price touched EMA9 in an uptrend
        self.touch_dn = 99
        self._day = None
        self._traded = False
        self._entry_spot = None
        self._side = None

    def meta(self) -> StrategyMeta:
        return StrategyMeta(
            name="PBK Confluence",
            underlying="NIFTY",
            segment="NSE_FNO",
            timeframe="5",
            params=self.params,
            description="Pullback-continuation buyer: trend + EMA9 touch + "
                        "resumption close + ATR conviction (10:00-14:30)",
        )

    def _update(self, bar):
        c = bar.close
        self.e9 = c if self.e9 is None else c * 0.2 + self.e9 * 0.8
        self.e21 = c if self.e21 is None else c * (2.0 / 22) + self.e21 * (20.0 / 22)
        tr = bar.high - bar.low
        if self.prev_close is not None:
            tr = max(tr, abs(bar.high - self.prev_close), abs(bar.low - self.prev_close))
        self.atr = tr if self.atr is None else tr * (2.0 / 21) + self.atr * (19.0 / 21)
        self.prev_close = c
        if self.e9 > self.e21:
            self.align = self.align + 1 if self.align >= 0 else 1
            self.touch_up = 0 if bar.low <= self.e9 else self.touch_up + 1
            self.touch_dn = 99
        elif self.e9 < self.e21:
            self.align = self.align - 1 if self.align <= 0 else -1
            self.touch_dn = 0 if bar.high >= self.e9 else self.touch_dn + 1
            self.touch_up = 99

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        t = ctx.now.time()
        hm = (t.hour, t.minute)
        d = ctx.now.date()
        if self._day != d:
            self._day = d
            self._traded = False
            self.prev_bar = None       # no overnight-gap resumption signals
            self.touch_up = self.touch_dn = 99

        # ---- manage open position (uses last completed-bar state) ---------
        if ctx.positions:
            move = ctx.spot - self._entry_spot
            fav = move if self._side == "up" else -move
            if fav >= self.params["target_pts"]:
                ctx.log(f"target +{fav:.1f} pts")
                ctx.exit_all(reason="target")
            elif fav <= -self.params["stop_pts"]:
                ctx.log(f"stop {fav:.1f} pts")
                ctx.exit_all(reason="stop_loss")
            elif hm >= (15, 0):
                ctx.log("time exit 15:00")
                ctx.exit_all(reason="time_exit")
        elif (not self._traded and (10, 0) <= hm <= (14, 30)
              and self.prev_bar is not None and self.atr is not None):
            rng = bar.high - bar.low
            conviction = rng >= self.params["atr_mult"] * self.atr
            side = None
            if (self.align >= 6 and self.touch_up <= 3 and conviction
                    and bar.close > self.prev_bar.high):
                side = "up"
            elif (self.align <= -6 and self.touch_dn <= 3 and conviction
                    and bar.close < self.prev_bar.low):
                side = "dn"
            if side:
                ot = OptionType.CALL if side == "up" else OptionType.PUT
                ok = ctx.enter([LegSpec(ot, Action.BUY, strike_offset=0,
                                        expiry_kind=ExpiryKind.WEEKLY,
                                        lots=self.params["lots"], tag="pbk")],
                               tag="pbk", sl_pct=self.params["sl_pct"])
                if ok:
                    self._traded = True
                    self._entry_spot = ctx.spot
                    self._side = side
                    ctx.log(f"PBK {side} @ spot {ctx.spot:.1f}")

        self._update(bar)
        self.prev_bar = bar

    def on_day_end(self, ctx: Context) -> None:
        if ctx.positions:
            ctx.exit_all(reason="time_exit")
