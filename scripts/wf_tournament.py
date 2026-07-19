#!/usr/bin/env python3
"""
Walk-Forward Tournament: PBK v1 vs v2 vs Regime Rider
======================================================
Validates three strategy candidates on real NIFTY data via 4-fold walk-forward.
Pre-registered bar: OOS expectancy must beat PBK v1 baseline (+2.2 pts/trade).

Usage:
  python3 scripts/wf_tournament.py

Output: tournament_results.json with per-fold OOS metrics + aggregate curves.
"""

import json
import sys
from datetime import datetime, time
from pathlib import Path

# Add app to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.contract import (
    Strategy, Context, Bar, StrategyMeta, OptionType, Action,
    LegSpec, ExpiryKind, Position
)
from app.data.store import DataStore, SyntheticStore
from app.engines import walkforward


# ============================================================================
# Strategy 1: PBK Confluence v1 (baseline, no params)
# ============================================================================

class PBKConfluence(Strategy):
    """Baseline: buy the dip in an established intraday trend."""

    def __init__(self):
        self.params = {"target_pts": 50, "stop_pts": 50, "atr_mult": 1.2,
                       "sl_pct": 0.45, "lots": 1}
        self.e9 = None
        self.e21 = None
        self.atr = None
        self.prev_close = None
        self.prev_bar = None
        self.align = 0
        self.touch_up = 99
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
            self.prev_bar = None
            self.touch_up = self.touch_dn = 99

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
            ctx.exit_all()


# ============================================================================
# Strategy 2: PBK Confluence v2 (with optional filters)
# ============================================================================

class PBKConfluenceV2(Strategy):
    """PBK with extension/backstop/trend-age quality gates."""

    def __init__(self):
        self.params = {"target_pts": 50, "stop_pts": 50, "atr_mult": 1.2,
                       "sl_pct": 0.45, "lots": 1,
                       "ext_max_atr": 0.0,
                       "backstop_pts": 0.0,
                       "min_align": 6,
                       "max_recross_11": 0}
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
        self._traded = False
        self._entry_spot = None
        self._side = None
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
        self.prev_bar = None
        self.touch_up = self.touch_dn = 99

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
        if side == "up":
            below = [w for w in walls if w <= c]
            return bool(below) and c - max(below) <= pts
        above = [w for w in walls if w >= c]
        return bool(above) and min(above) - c <= pts

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


# ============================================================================
# Strategy 3: Regime Rider (new candidate from 30 annotated charts)
# ============================================================================

class RegimeRider(Strategy):
    """Trade WITH the day's commitment at its structure tests."""

    def __init__(self):
        self.params = {"stop_pts": 50, "target_pts": 0,
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

    def _commitment(self, c):
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


# ============================================================================
# Tournament Setup
# ============================================================================

def run_tournament():
    """Execute 4-fold walk-forward tournament on all three candidates."""

    # Detect store availability
    try:
        store = DataStore()
        # Check if store has data
        result = store._q1("SELECT COUNT(*) FROM underlying_bars WHERE underlying = 'NIFTY'")
        if result and result[0] > 0:
            print("✓ Using real data store")
            use_real_store = True
        else:
            print("! Real store is empty; using synthetic data")
            store = SyntheticStore()
            use_real_store = False
    except Exception as e:
        print(f"! Could not load real store ({e}); using synthetic data")
        store = SyntheticStore()
        use_real_store = False

    # Date range: use last 2 years of available data
    if use_real_store:
        result = store._q1(
            "SELECT MIN(ts), MAX(ts) FROM underlying_bars WHERE underlying = 'NIFTY'"
        )
        if result and result[0]:
            start_dt = result[0]
            end_dt = result[1]
        else:
            print("No NIFTY data found; cannot run tournament")
            return
    else:
        # Synthetic store: use a fixed 2-year window
        end_dt = datetime(2026, 7, 15, 15, 30)
        start_dt = datetime(2024, 7, 15, 9, 15)

    print(f"Tournament dates: {start_dt.date()} to {end_dt.date()}")
    print(f"Data range: ~{(end_dt.date() - start_dt.date()).days} days")

    # Strategy factories
    def pbk_v1_factory(params):
        s = PBKConfluence()
        s.params.update(params)
        return s

    def pbk_v2_factory(params):
        s = PBKConfluenceV2()
        s.params.update(params)
        return s

    def regime_rider_factory(params):
        s = RegimeRider()
        s.params.update(params)
        return s

    results = {}
    candidates = [
        ("PBK v1 (baseline)", pbk_v1_factory, {}),
        ("PBK v2 (filtered)", pbk_v2_factory, {
            "ext_max_atr": [1.0, 1.5, 2.0],
            "backstop_pts": [20, 40, 60],
            "min_align": [6, 8, 10],
        }),
        ("Regime Rider", regime_rider_factory, {}),
    ]

    for name, factory, grid in candidates:
        print(f"\n{'='*70}")
        print(f"Running: {name}")
        print(f"{'='*70}")

        def progress(done, total, msg):
            pct = round(100 * done / total) if total else 0
            print(f"  [{pct:3d}%] {msg}")

        try:
            result = walkforward.run_walkforward(
                factory, store, "NIFTY",
                start_dt, end_dt,
                folds=4,
                is_frac=0.7,
                param_grid=grid or None,
                capital=1_000_000.0,
                metric="sharpe",
                max_runs=300,
                on_progress=progress
            )
            results[name] = result
        except Exception as e:
            print(f"  ✗ Error: {e}")
            results[name] = {"status": "error", "message": str(e)}

    # Print summary
    print(f"\n{'='*70}")
    print("TOURNAMENT SUMMARY")
    print(f"{'='*70}\n")

    baseline_oos = results.get("PBK v1 (baseline)", {}).get("aggregate_oos", {})
    baseline_return = baseline_oos.get("return_pct", 0)
    baseline_dd = baseline_oos.get("max_drawdown_pct", 0)
    baseline_days = baseline_oos.get("days", 0)

    print(f"PBK v1 (baseline) OOS:")
    print(f"  Return:      {baseline_return:+.2f}%")
    print(f"  Max Drawdown: {baseline_dd:.2f}%")
    print(f"  Days:        {baseline_days}")
    print()

    for name in ["PBK v2 (filtered)", "Regime Rider"]:
        res = results.get(name, {})
        if res.get("status") == "error":
            print(f"{name}: ERROR — {res.get('message')}")
            print()
            continue

        oos = res.get("aggregate_oos", {})
        oos_return = oos.get("return_pct", 0)
        oos_dd = oos.get("max_drawdown_pct", 0)
        oos_days = oos.get("days", 0)

        delta = oos_return - baseline_return
        delta_str = f"+{delta:.2f}%" if delta > 0 else f"{delta:.2f}%"
        verdict = "✓ PASS" if delta > 0 else "✗ FAIL"

        print(f"{name}: {verdict}")
        print(f"  Return:      {oos_return:+.2f}% (baseline: {baseline_return:+.2f}%, delta: {delta_str})")
        print(f"  Max Drawdown: {oos_dd:.2f}% (baseline: {baseline_dd:.2f}%)")
        print(f"  Days:        {oos_days}")
        print()

    # Save results
    output_path = Path("tournament_results.json")
    with output_path.open("w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"Full results saved to: {output_path}")


if __name__ == "__main__":
    run_tournament()
