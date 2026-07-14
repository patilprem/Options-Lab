# PBK Confluence v2 — same archetype as pbk_confluence.py plus THREE entry
# quality filters, each a param DEFAULTING OFF (0), so with defaults this
# behaves bar-for-bar like v1. Motivation: 2026-07 live stop-out — v1 bought
# a resumption ~2-3 ATR below VWAP right after a waterfall (chasing maximum
# extension, no structure behind the entry) and its one-trade-per-day was
# spent before the textbook pullback-into-confluence setup at midday.
#
#   ext_max_atr   skip entries further than this many ATRs from session VWAP
#                 (0 = off; try 1.0-2.0 in walk-forward)
#   backstop_pts  require a structural wall (EMA21 / VWAP / daily pivot) within
#                 this many points BEHIND the entry (0 = off; try 20-60)
#   min_align     consecutive aligned EMA bars required (6 = v1 behavior;
#                 try 8-12 to distrust the opening drive on gap days)
#
# Evaluate v1 vs v2 on real data (Backtest tab / Walk-Forward) before ever
# deploying v2. Adopt filters only if OOS expectancy improves.

class PBKConfluenceV2(Strategy):
    """Buy the dip in an established intraday trend — with optional
    extension / backstop / trend-age quality gates.

    Core entry (long; short is the mirror), all on one 5-min bar:
      * EMA9 above EMA21 for the last `min_align` bars
      * price touched EMA9 within the last 3 bars (the pullback)
      * this bar closes above the previous bar's high (resumption)
      * this bar's range >= 1.2 x ATR20 (conviction)
      * 10:00-14:30, one trade/day
    Optional gates: not overextended vs VWAP; a wall close behind the entry.
    Exit: +target/-stop spot points, else hard flat 15:00. Premium SL net.
    """

    def __init__(self):
        self.params = {"target_pts": 50, "stop_pts": 50, "atr_mult": 1.2,
                       "sl_pct": 0.45, "lots": 1,
                       "ext_max_atr": 0.0,     # 0 = off
                       "backstop_pts": 0.0,    # 0 = off
                       "min_align": 6,
                       "max_recross_11": 0}    # 0 = off (chop-day gate)
        self.e9 = None
        self.e21 = None
        self.atr = None
        self.prev_close = None
        self.prev_bar = None
        self.align = 0
        self.touch_up = 99
        self.touch_dn = 99
        # session vwap (index bars carry no volume -> typical-price average)
        self._pv = 0.0
        self._v = 0.0
        # daily pivot from the previous session (for the backstop wall list)
        self._day = None
        self._day_h = self._day_l = self._day_c = None
        self.pivot = None
        self._traded = False
        self._entry_spot = None
        self._side = None
        # chop detector (study 2026-07: >=3 pivot recrosses by 11:00 ->
        # median trend efficiency 0.38 vs 0.48 — the one hypothesis that
        # survived; gate is OFF by default pending walk-forward)
        self._recross_11 = 0
        self._pivot_side = None

    def meta(self) -> StrategyMeta:
        return StrategyMeta(
            name="PBK Confluence v2",
            underlying="NIFTY",
            segment="NSE_FNO",
            timeframe="5",
            params=self.params,
            description="PBK pullback-continuation + optional extension/"
                        "backstop/trend-age entry filters (defaults = v1)",
        )

    # ---- indicators ----------------------------------------------------------

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

    @property
    def vwap(self):
        return self._pv / self._v if self._v else None

    def _roll_day(self, d):
        if self._day is not None and self._day_h is not None:
            self.pivot = (self._day_h + self._day_l + self._day_c) / 3.0
        self._day = d
        self._day_h = self._day_l = self._day_c = None
        self._pv = self._v = 0.0
        self._traded = False
        self._recross_11 = 0
        self._pivot_side = None
        self.prev_bar = None       # no overnight-gap resumption signals
        self.touch_up = self.touch_dn = 99

    # ---- quality gates (each inert when its param is 0) ----------------------

    def _not_overextended(self, c):
        m = float(self.params["ext_max_atr"])
        if not m or self.vwap is None or not self.atr:
            return True
        return abs(c - self.vwap) <= m * self.atr

    def _has_backstop(self, c, side):
        pts = float(self.params["backstop_pts"])
        if not pts:
            return True
        walls = [w for w in (self.e21, self.vwap, self.pivot) if w is not None]
        if side == "up":       # long: wall must sit just BELOW the entry
            below = [w for w in walls if w <= c]
            return bool(below) and c - max(below) <= pts
        above = [w for w in walls if w >= c]
        return bool(above) and min(above) - c <= pts

    # ---- main hook ------------------------------------------------------------

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        t = ctx.now.time()
        hm = (t.hour, t.minute)
        d = ctx.now.date()
        if d != self._day:
            self._roll_day(d)
        self._day_h = bar.high if self._day_h is None else max(self._day_h, bar.high)
        self._day_l = bar.low if self._day_l is None else min(self._day_l, bar.low)
        self._day_c = bar.close
        w = bar.volume if bar.volume and bar.volume > 0 else 1.0
        self._pv += w * (bar.high + bar.low + bar.close) / 3.0
        self._v += w
        if self.pivot is not None and hm <= (11, 0):
            side_now = bar.close >= self.pivot
            if self._pivot_side is not None and side_now != self._pivot_side:
                self._recross_11 += 1
            self._pivot_side = side_now

        # ---- manage open position (uses last completed-bar state) -----------
        if ctx.positions:
            move = ctx.spot - self._entry_spot
            fav = move if self._side == "up" else -move
            if fav >= self.params["target_pts"]:
                ctx.log(f"target +{fav:.1f} pts")
                ctx.exit_all()
            elif fav <= -self.params["stop_pts"]:
                ctx.log(f"stop {fav:.1f} pts")
                ctx.exit_all()
            elif hm >= (15, 0):
                ctx.log("time exit 15:00")
                ctx.exit_all()
        elif (not self._traded and (10, 0) <= hm <= (14, 30)
              and self.prev_bar is not None and self.atr is not None):
            rng = bar.high - bar.low
            conviction = rng >= self.params["atr_mult"] * self.atr
            need = int(self.params["min_align"])
            side = None
            if (self.align >= need and self.touch_up <= 3 and conviction
                    and bar.close > self.prev_bar.high):
                side = "up"
            elif (self.align <= -need and self.touch_dn <= 3 and conviction
                    and bar.close < self.prev_bar.low):
                side = "dn"
            if side and not self._not_overextended(bar.close):
                ctx.log(f"skip {side}: >|{self.params['ext_max_atr']}| ATR from vwap")
                side = None
            if side and not self._has_backstop(bar.close, side):
                ctx.log(f"skip {side}: no wall within {self.params['backstop_pts']} pts")
                side = None
            mrc = int(self.params["max_recross_11"])
            if side and mrc and self._recross_11 >= mrc:
                ctx.log(f"skip {side}: chop day ({self._recross_11} pivot "
                        f"recrosses by 11:00)")
                side = None
            if side:
                ot = OptionType.CALL if side == "up" else OptionType.PUT
                ok = ctx.enter([LegSpec(ot, Action.BUY, strike_offset=0,
                                        expiry_kind=ExpiryKind.WEEKLY,
                                        lots=int(self.params["lots"]), tag="pbk2")],
                               tag="pbk2", sl_pct=self.params["sl_pct"])
                if ok:
                    self._traded = True
                    self._entry_spot = ctx.spot
                    self._side = side
                    ctx.log(f"PBK2 {side} @ spot {ctx.spot:.1f}")

        self._update(bar)
        self.prev_bar = bar

    def on_day_end(self, ctx: Context) -> None:
        if ctx.positions:
            ctx.exit_all()
