# Pivot Confluence Seller — trend-following premium seller (NIFTY, 5-min).
# Sells the side the trend has abandoned, only when FOUR independent reads
# agree: Supertrend direction, price vs session VWAP, price vs EMA20, and
# price vs the classic daily pivot. Paste into New Strategy.
#
# Needs one prior session for pivots (tracked internally) — day 1 after a
# deploy/restart it won't trade. Index bars carry no volume, so VWAP
# degrades gracefully to the session average of typical price.

class PivotConfluenceSeller(Strategy):
    """Sell an OTM weekly option WITH the intraday trend.

    Bullish day (mirror for bearish): Supertrend UP, close above session
    VWAP, above EMA20, and above the daily pivot P -> SELL a PUT
    `otm_offset` strikes below ATM. The short leg is backed by structure:
    the pivot and VWAP sit between spot and the strike.

    Exits (first to trigger):
      * engine-enforced premium stop (+sl_pct) and target (-target_pct)
      * Supertrend flips against the position
      * close crosses back through the daily pivot (structure broken)
      * hard flat at 15:10
    One trade per day, entries 10:00-14:00 only.
    """

    def __init__(self):
        self.params = {"st_period": 10, "st_mult": 3.0, "ema_period": 20,
                       "otm_offset": 2, "sl_pct": 0.50, "target_pct": 0.50,
                       "lots": 1}
        # supertrend state
        self.atr = None
        self.fub = None          # final upper band
        self.flb = None          # final lower band
        self.st_up = True
        self.prev_close = None
        self.warmup = 0
        # ema
        self.ema = None
        # session vwap (volume falls back to 1/bar on indexes)
        self._pv = 0.0
        self._v = 0.0
        # daily pivots from the PREVIOUS session (tracked here)
        self._day = None
        self._day_h = self._day_l = self._day_c = None
        self.pivot = None
        self.r1 = self.s1 = None
        self._traded = False
        self._side = None        # "up" -> short PUT, "dn" -> short CALL

    def meta(self) -> StrategyMeta:
        return StrategyMeta(
            name="Pivot Confluence Seller",
            underlying="NIFTY",
            segment="NSE_FNO",
            timeframe="5",
            params=self.params,
            description="Sell OTM option with the trend: Supertrend + VWAP "
                        "+ EMA20 + daily pivot all aligned (10:00-14:00)",
        )

    # ---- indicators --------------------------------------------------------

    def _update_supertrend(self, bar):
        n, mult = self.params["st_period"], self.params["st_mult"]
        h, l, c = bar.high, bar.low, bar.close
        tr = h - l
        if self.prev_close is not None:
            tr = max(tr, abs(h - self.prev_close), abs(l - self.prev_close))
        self.atr = tr if self.atr is None else (self.atr * (n - 1) + tr) / n
        mid = (h + l) / 2.0
        ub = mid + mult * self.atr
        lb = mid - mult * self.atr
        pc = self.prev_close if self.prev_close is not None else c
        # band carryover (standard supertrend rules)
        self.fub = ub if (self.fub is None or ub < self.fub or pc > self.fub) else self.fub
        self.flb = lb if (self.flb is None or lb > self.flb or pc < self.flb) else self.flb
        if self.st_up and c < self.flb:
            self.st_up = False
        elif not self.st_up and c > self.fub:
            self.st_up = True
        self.prev_close = c
        self.warmup += 1

    def _update_ema(self, close):
        n = self.params["ema_period"]
        k = 2.0 / (n + 1)
        self.ema = close if self.ema is None else close * k + self.ema * (1 - k)

    def _update_vwap(self, bar):
        w = bar.volume if bar.volume and bar.volume > 0 else 1.0
        self._pv += w * (bar.high + bar.low + bar.close) / 3.0
        self._v += w

    @property
    def vwap(self):
        return self._pv / self._v if self._v else None

    def _roll_day(self, d):
        if self._day is not None and self._day_h is not None:
            p = (self._day_h + self._day_l + self._day_c) / 3.0
            self.pivot = p
            self.r1 = 2 * p - self._day_l
            self.s1 = 2 * p - self._day_h
        self._day = d
        self._day_h = self._day_l = self._day_c = None
        self._pv = self._v = 0.0
        self._traded = False
        self._side = None

    def _track_day(self, bar):
        self._day_h = bar.high if self._day_h is None else max(self._day_h, bar.high)
        self._day_l = bar.low if self._day_l is None else min(self._day_l, bar.low)
        self._day_c = bar.close

    # ---- main hook ---------------------------------------------------------

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        d = ctx.now.date()
        if d != self._day:
            self._roll_day(d)
        self._track_day(bar)
        self._update_vwap(bar)

        t = ctx.now.time()
        hm = (t.hour, t.minute)
        c = bar.close

        # ---- manage an open position (uses last completed-bar state) ------
        if ctx.positions:
            flipped = (self._side == "up" and not self.st_up) or \
                      (self._side == "dn" and self.st_up)
            broke_pivot = self.pivot is not None and (
                (self._side == "up" and c < self.pivot) or
                (self._side == "dn" and c > self.pivot))
            if flipped:
                ctx.log("exit: supertrend flipped")
                ctx.exit_all()
            elif broke_pivot:
                ctx.log(f"exit: pivot {self.pivot:.0f} broken @ {c:.1f}")
                ctx.exit_all()
            elif hm >= (15, 10):
                ctx.log("time exit 15:10")
                ctx.exit_all()
        # ---- entry ---------------------------------------------------------
        elif (not self._traded and (10, 0) <= hm <= (14, 0)
              and self.pivot is not None
              and self.warmup >= 2 * self.params["st_period"]
              and self.ema is not None and self.vwap is not None):
            side = None
            if self.st_up and c > self.vwap and c > self.ema and c > self.pivot:
                side = "up"
            elif (not self.st_up) and c < self.vwap and c < self.ema and c < self.pivot:
                side = "dn"
            if side:
                off = int(self.params["otm_offset"])
                leg = (LegSpec(OptionType.PUT, Action.SELL, strike_offset=-off,
                               expiry_kind=ExpiryKind.WEEKLY,
                               lots=int(self.params["lots"]), tag="short_pe")
                       if side == "up" else
                       LegSpec(OptionType.CALL, Action.SELL, strike_offset=+off,
                               expiry_kind=ExpiryKind.WEEKLY,
                               lots=int(self.params["lots"]), tag="short_ce"))
                ok = ctx.enter([leg], tag="pcs",
                               sl_pct=self.params["sl_pct"],
                               target_pct=self.params["target_pct"])
                if ok:
                    self._traded = True
                    self._side = side
                    ctx.log(f"PCS {side}: sold {'PE' if side == 'up' else 'CE'} "
                            f"@ spot {ctx.spot:.1f} (P {self.pivot:.0f}, "
                            f"vwap {self.vwap:.0f}, ema {self.ema:.0f}, "
                            f"ST {'up' if self.st_up else 'dn'})")

        # indicators update AFTER decisions (decisions use completed-bar state)
        self._update_supertrend(bar)
        self._update_ema(c)

    def on_day_end(self, ctx: Context) -> None:
        if ctx.positions:
            ctx.exit_all()
