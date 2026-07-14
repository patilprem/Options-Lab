# PBK Seller — pullback-REJECTION premium seller (NIFTY, 5-min).
# The selling mirror of PBK Confluence, with the 2026-07 live observations
# built in as CORE rules (not optional params):
#
#   1. WALL REQUIRED: the rejection must happen AT structure — entry only
#      when the pullback stalls within `backstop_pts` of a confluence wall
#      (EMA21 / session VWAP / daily pivot). The short strike then sits on
#      the far side of that wall, which stands guard between spot and it.
#   2. NO CHASING: entries further than `ext_max_atr` ATRs from VWAP are
#      refused — sell the rejection near the mean, never after the move
#      has already run (the exact mistake that stopped PBK out at 10:30).
#   3. DON'T WASTE THE DAY: up to `max_trades` setups/day with a cooldown,
#      so an early stop doesn't forfeit the textbook midday setup.
#
# Paste into New Strategy. Day 1 after deploy: pivot is unknown, so the
# wall list is EMA21 + VWAP only (still trades). Index bars carry no
# volume -> VWAP degrades to the session average of typical price.

class PBKSeller(Strategy):
    """Sell the option on the far side of a defended wall.

    Bearish entry (mirror for bullish), all on one 5-min bar:
      * EMA9 below EMA21 for the last `min_align` bars (trend established)
      * price pulled back UP to touch EMA9 within the last 3 bars
      * this bar closes below the previous bar's low (rejection confirmed)
      * the rejection happened within `backstop_pts` under a wall
        (EMA21 / VWAP / daily pivot) -> SELL CALL `otm_offset` strikes up
      * close within `ext_max_atr` ATRs of VWAP (no chasing)
      * 10:00-14:30, at most `max_trades` per day, cooldown between trades
    Exits: engine premium SL/target, close crossing back through the wall
    (+buffer), EMA flip, hard flat 15:10.
    """

    def __init__(self):
        self.params = {"min_align": 6, "ext_max_atr": 2.0, "backstop_pts": 40,
                       "wall_buffer_pts": 10, "otm_offset": 2,
                       "sl_pct": 0.50, "target_pct": 0.50,
                       "max_trades": 2, "cooldown_bars": 6, "lots": 1}
        self.e9 = None
        self.e21 = None
        self.atr = None
        self.prev_close = None
        self.prev_bar = None
        self.align = 0
        self.touch_up = 99      # bars since pullback-touch in an uptrend
        self.touch_dn = 99      # bars since pullback-touch in a downtrend
        # session vwap
        self._pv = 0.0
        self._v = 0.0
        # daily pivot from previous session
        self._day = None
        self._day_h = self._day_l = self._day_c = None
        self.pivot = None
        # trade management
        self._trades_today = 0
        self._cool = 0
        self._was_in_pos = False
        self._side = None       # "dn" -> short CALL, "up" -> short PUT
        self._wall = None       # structure level defended at entry

    def meta(self) -> StrategyMeta:
        return StrategyMeta(
            name="PBK Seller",
            underlying="NIFTY",
            segment="NSE_FNO",
            timeframe="5",
            params=self.params,
            description="Pullback-rejection premium seller: sell OTM option "
                        "behind an EMA21/VWAP/pivot wall, no chasing, "
                        "up to 2 setups/day (10:00-14:30)",
        )

    # ---- indicators -----------------------------------------------------------

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
        self._trades_today = 0
        self._cool = 0
        self.prev_bar = None
        self.touch_up = self.touch_dn = 99

    # ---- today's observations as gates ---------------------------------------

    def _near_mean(self, c):
        if self.vwap is None or not self.atr:
            return False
        return abs(c - self.vwap) <= self.params["ext_max_atr"] * self.atr

    def _wall_behind(self, c, side):
        """Nearest structure level on the adverse side, if close enough to
        act as the trade's bodyguard. Returns the level or None."""
        pts = self.params["backstop_pts"]
        walls = [w for w in (self.e21, self.vwap, self.pivot) if w is not None]
        if side == "dn":       # short CALL: wall must cap price from above
            above = [w for w in walls if w >= c]
            if above and min(above) - c <= pts:
                return min(above)
        else:                  # short PUT: wall must support price from below
            below = [w for w in walls if w <= c]
            if below and c - max(below) <= pts:
                return max(below)
        return None

    # ---- main hook -------------------------------------------------------------

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        d = ctx.now.date()
        if d != self._day:
            self._roll_day(d)
        self._day_h = bar.high if self._day_h is None else max(self._day_h, bar.high)
        self._day_l = bar.low if self._day_l is None else min(self._day_l, bar.low)
        self._day_c = bar.close
        w = bar.volume if bar.volume and bar.volume > 0 else 1.0
        self._pv += w * (bar.high + bar.low + bar.close) / 3.0
        self._v += w

        t = ctx.now.time()
        hm = (t.hour, t.minute)
        c = bar.close

        # cooldown ticks after ANY exit, incl. engine-enforced premium SL
        if self._was_in_pos and not ctx.positions:
            self._cool = int(self.params["cooldown_bars"])
        elif self._cool > 0 and not ctx.positions:
            self._cool -= 1

        if ctx.positions:
            buf = self.params["wall_buffer_pts"]
            wall_broken = self._wall is not None and (
                (self._side == "dn" and c > self._wall + buf) or
                (self._side == "up" and c < self._wall - buf))
            ema_flip = (self._side == "dn" and self.e9 is not None
                        and self.e21 is not None and self.e9 > self.e21) or \
                       (self._side == "up" and self.e9 is not None
                        and self.e21 is not None and self.e9 < self.e21)
            if wall_broken:
                ctx.log(f"exit: wall {self._wall:.0f} broken @ {c:.1f}")
                ctx.exit_all()
            elif ema_flip:
                ctx.log("exit: EMA flip")
                ctx.exit_all()
            elif hm >= (15, 10):
                ctx.log("time exit 15:10")
                ctx.exit_all()
        elif (self._trades_today < int(self.params["max_trades"])
              and self._cool == 0 and (10, 0) <= hm <= (14, 30)
              and self.prev_bar is not None and self.atr is not None):
            need = int(self.params["min_align"])
            side = None
            if (self.align <= -need and self.touch_dn <= 3
                    and bar.close < self.prev_bar.low):
                side = "dn"
            elif (self.align >= need and self.touch_up <= 3
                    and bar.close > self.prev_bar.high):
                side = "up"
            if side and not self._near_mean(c):
                ctx.log(f"skip {side}: too far from vwap (chasing)")
                side = None
            wall = self._wall_behind(c, side) if side else None
            if side and wall is None:
                ctx.log(f"skip {side}: no wall within "
                        f"{self.params['backstop_pts']} pts")
                side = None
            if side:
                off = int(self.params["otm_offset"])
                leg = (LegSpec(OptionType.CALL, Action.SELL, strike_offset=+off,
                               expiry_kind=ExpiryKind.WEEKLY,
                               lots=int(self.params["lots"]), tag="short_ce")
                       if side == "dn" else
                       LegSpec(OptionType.PUT, Action.SELL, strike_offset=-off,
                               expiry_kind=ExpiryKind.WEEKLY,
                               lots=int(self.params["lots"]), tag="short_pe"))
                ok = ctx.enter([leg], tag="pbks",
                               sl_pct=self.params["sl_pct"],
                               target_pct=self.params["target_pct"])
                if ok:
                    self._trades_today += 1
                    self._side = side
                    self._wall = wall
                    ctx.log(f"PBKS {side}: sold {'CE' if side == 'dn' else 'PE'} "
                            f"@ spot {ctx.spot:.1f} behind wall {wall:.0f} "
                            f"(trade {self._trades_today}/"
                            f"{int(self.params['max_trades'])})")

        self._was_in_pos = bool(ctx.positions)
        self._update(bar)
        self.prev_bar = bar

    def on_day_end(self, ctx: Context) -> None:
        if ctx.positions:
            ctx.exit_all()
