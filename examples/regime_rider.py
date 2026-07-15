# Regime Rider — day-regime continuation buyer (NIFTY, 5-min).
# The synthesis of ~30 annotated charts (Jun 16 - Jul 15 2026) and the
# structure study on 262 real sessions. Every winning circle was the same
# trade: the day COMMITS to a direction, price makes an orderly first
# return to the mean structure, the test FAILS with the commitment, the
# day continues. Every quantitatively dead idea was a reversal against
# commitment (extension fades: 50-54% vs pre-registered 56% bar, 3 configs).
#
# Evidence -> rule mapping (all ON by default; this strategy IS its gates):
#   * chop gate      recross_11 >= 3 -> no-trade day (the study's lone
#                    survivor: trend_eff 0.38 vs 0.48)
#   * commitment     EMA9/21 alignment AND pivot side must AGREE
#   * no chasing     entries within 2 ATR of session VWAP (the Jul-14 stop
#                    was a 2-3 ATR chase after a waterfall)
#   * wall behind    EMA21/VWAP/pivot within 40 pts on the adverse side
#                    (every circled winner had structure at its back)
#   * two bullets    an early stop must not forfeit the midday A+ setup
#                    (Jul 14/15: the one-bullet problem, three live cases)
#   * trailing exit  waterfalls ran 150-400 pts; a fixed +50 cut them short.
#                    Default rides until EMA flip / wall break / 15:00
#                    (target_pts > 0 restores a fixed target for comparison)
#
# NOT YET VALIDATED. Paste into New Strategy for backtests; deploy only
# after it beats PBK v1 out-of-sample in the walk-forward tournament.

class RegimeRider(Strategy):
    """Trade WITH the day's commitment at its structure tests.

    Regime (recomputed every bar, entries from 10:30):
      * >=3 pivot recrosses by 11:00 -> chop day, stand aside entirely
      * commitment = 'up' iff EMA9>EMA21 for >=min_align bars AND close>pivot
        (mirror for 'dn'); disagreement -> stand aside
    Entry (max_trades/day, cooldown after any exit):
      * pullback touched EMA9 within last 3 bars (orderly return to mean)
      * this bar closes beyond prev bar's extreme WITH the commitment
      * within ext_max_atr ATRs of VWAP; a wall within backstop_pts behind
      * -> BUY ATM weekly option in the commitment direction
    Exit: spot stop_pts against entry; EMA flip; wall break (+buffer);
    fixed target only if target_pts>0; hard flat 15:00. Premium SL net.
    """

    def __init__(self):
        self.params = {"stop_pts": 50, "target_pts": 0,   # 0 = trail, don't cap
                       "min_align": 6, "ext_max_atr": 2.0,
                       "backstop_pts": 40, "wall_buffer_pts": 10,
                       "max_recross_11": 3, "max_trades": 2,
                       "cooldown_bars": 6, "sl_pct": 0.45, "lots": 1}
        self.e9 = None
        self.e21 = None
        self.atr = None
        self.prev_close = None
        self.prev_bar = None
        self.align = 0
        self.touch_up = 99
        self.touch_dn = 99
        self._pv = 0.0
        self._v = 0.0
        self._day = None
        self._day_h = self._day_l = self._day_c = None
        self.pivot = None
        self._recross_11 = 0
        self._pivot_side = None
        self._chop_logged = False
        self._trades_today = 0
        self._cool = 0
        self._was_in_pos = False
        self._side = None
        self._entry_spot = None
        self._wall = None

    def meta(self) -> StrategyMeta:
        return StrategyMeta(
            name="Regime Rider",
            underlying="NIFTY",
            segment="NSE_FNO",
            timeframe="5",
            params=self.params,
            description="Day-regime continuation: chop gate + commitment "
                        "(EMA & pivot agree) + structure-test entries with "
                        "wall backing, 2 bullets, trailing exits",
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
        self._recross_11 = 0
        self._pivot_side = None
        self._chop_logged = False
        self._trades_today = 0
        self._cool = 0
        self.prev_bar = None
        self.touch_up = self.touch_dn = 99

    # ---- the evidence, as code -------------------------------------------------

    def _commitment(self, c):
        """'up' / 'dn' when EMA regime and pivot side AGREE, else None."""
        need = int(self.params["min_align"])
        if self.pivot is None:
            return None
        if self.align >= need and c > self.pivot:
            return "up"
        if self.align <= -need and c < self.pivot:
            return "dn"
        return None

    def _near_mean(self, c):
        if self.vwap is None or not self.atr:
            return False
        return abs(c - self.vwap) <= self.params["ext_max_atr"] * self.atr

    def _wall_behind(self, c, side):
        pts = self.params["backstop_pts"]
        walls = [w for w in (self.e21, self.vwap, self.pivot) if w is not None]
        if side == "up":
            below = [w for w in walls if w <= c]
            if below and c - max(below) <= pts:
                return max(below)
        else:
            above = [w for w in walls if w >= c]
            if above and min(above) - c <= pts:
                return min(above)
        return None

    # ---- main hook ---------------------------------------------------------------

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
        c = bar.close
        if self.pivot is not None and hm <= (11, 0):
            side_now = c >= self.pivot
            if self._pivot_side is not None and side_now != self._pivot_side:
                self._recross_11 += 1
            self._pivot_side = side_now

        if self._was_in_pos and not ctx.positions:
            self._cool = int(self.params["cooldown_bars"])
        elif self._cool > 0 and not ctx.positions:
            self._cool -= 1

        # ---- manage (uses last completed-bar state) --------------------------
        if ctx.positions:
            move = ctx.spot - self._entry_spot
            fav = move if self._side == "up" else -move
            tgt = float(self.params["target_pts"])
            buf = self.params["wall_buffer_pts"]
            wall_broken = self._wall is not None and (
                (self._side == "up" and c < self._wall - buf) or
                (self._side == "dn" and c > self._wall + buf))
            ema_flip = (self._side == "up" and self.align < 0) or \
                       (self._side == "dn" and self.align > 0)
            if fav <= -self.params["stop_pts"]:
                ctx.log(f"stop {fav:.1f} pts")
                ctx.exit_all()
            elif tgt > 0 and fav >= tgt:
                ctx.log(f"target +{fav:.1f} pts")
                ctx.exit_all()
            elif wall_broken:
                ctx.log(f"exit: wall {self._wall:.0f} broken (+{fav:.1f} pts)")
                ctx.exit_all()
            elif ema_flip:
                ctx.log(f"exit: EMA flip (+{fav:.1f} pts)")
                ctx.exit_all()
            elif hm >= (15, 0):
                ctx.log(f"time exit 15:00 ({fav:+.1f} pts)")
                ctx.exit_all()
        # ---- entries ----------------------------------------------------------
        elif (self._trades_today < int(self.params["max_trades"])
              and self._cool == 0 and (10, 30) <= hm <= (14, 15)
              and self.prev_bar is not None and self.atr is not None):
            if self._recross_11 >= int(self.params["max_recross_11"]):
                if not self._chop_logged:
                    ctx.log(f"chop day ({self._recross_11} recrosses by 11:00) "
                            "— standing aside")
                    self._chop_logged = True
            else:
                side = self._commitment(c)
                trigger = False
                if side == "up" and self.touch_up <= 3 and c > self.prev_bar.high:
                    trigger = True
                elif side == "dn" and self.touch_dn <= 3 and c < self.prev_bar.low:
                    trigger = True
                if trigger and not self._near_mean(c):
                    ctx.log(f"skip {side}: chasing (> "
                            f"{self.params['ext_max_atr']} ATR from vwap)")
                    trigger = False
                wall = self._wall_behind(c, side) if trigger else None
                if trigger and wall is None:
                    ctx.log(f"skip {side}: no wall within "
                            f"{self.params['backstop_pts']} pts")
                    trigger = False
                if trigger:
                    ot = OptionType.CALL if side == "up" else OptionType.PUT
                    ok = ctx.enter([LegSpec(ot, Action.BUY, strike_offset=0,
                                            expiry_kind=ExpiryKind.WEEKLY,
                                            lots=int(self.params["lots"]),
                                            tag="rider")],
                                   tag="rider", sl_pct=self.params["sl_pct"])
                    if ok:
                        self._trades_today += 1
                        self._side = side
                        self._entry_spot = ctx.spot
                        self._wall = wall
                        ctx.log(f"RIDER {side} @ {ctx.spot:.1f} wall {wall:.0f} "
                                f"(trade {self._trades_today}/"
                                f"{int(self.params['max_trades'])})")

        self._was_in_pos = bool(ctx.positions)
        self._update(bar)
        self.prev_bar = bar

    def on_day_end(self, ctx: Context) -> None:
        if ctx.positions:
            ctx.exit_all()
