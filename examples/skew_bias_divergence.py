# Example: trade the DIVERGENCE between the option surface and index breadth.
#
# Thesis (observed 2026-08-17 on NIFTY and BANKNIFTY, n=1 — see the warning in
# meta().description): while spot was pinned near the day's low, iv_skew fell
# hard on both indices (NIFTY -1.08 -> -2.28, BANKNIFTY -0.49 -> -1.44). skew
# is OTM-put IV minus OTM-call IV, so falling skew = CALLS richening relative
# to puts. Rising call OI *with* richening call IV is accumulation, not
# writing — writing supply pushes IV down. At the same time index_bias sat
# bearish (-0.58) on both names. The option surface was positioning long while
# constituent breadth said short; price then rallied ~145 NIFTY points.
#
# So the trigger is a DISAGREEMENT, and the trade sides with the option
# surface against the breadth read.
#
# STATUS: UNVALIDATED. The thesis above came from ONE session. Before this
# deserves capital, run the historical study over every recorded day:
#     GET /data/signal_study?start=...&end=...&underlying=NIFTY
#     (or venv/bin/python -m scripts.signal_study --start ... --end ...)
# It reports the condition's forward returns against the UNCONDITIONAL
# baseline over the same bars — the EDGE column — and sweeps the thresholds
# so a real plateau can be told apart from one lucky cell. If edge shows up in
# only a few grid cells, that is what noise looks like and this file should be
# deleted rather than tuned. The defaults below are the ONE-DAY observation
# written down, NOT a fitted result; set them from the study's plateau.
#
# HONESTY: this strategy CANNOT degrade gracefully. Its entire thesis is the
# two signals disagreeing, so with either unavailable it simply does not
# trade — no chain, no bias, no entry. In backtest that means it only trades
# over the window the recorder was actually running. That is a real
# limitation, not a bug: a version that "falls back to price action" would be
# a different strategy wearing this one's name.

class SkewBiasDivergence(Strategy):
    """Buy an ATM option when the option surface and index breadth disagree.

    Falling skew (calls bid up) + bearish breadth -> buy CALL.
    Rising skew (puts bid up) + bullish breadth  -> buy PUT.
    Both legs of the disagreement are required; either one alone does nothing.
    """

    def __init__(self):
        self.params = {
            # --- the signal ---
            "lookback_bars": 9,        # 9 x 5min = 45min, the run-up we measured
            "min_skew_shift": 0.50,    # vol points of skew movement required
            "bias_gate": 0.30,         # |index_bias| must be at least this to
                                       # count as a real disagreement
            # --- confirmation: buy the turn, don't chase it ---
            "max_range_pos": 0.45,     # for a CALL, spot must sit in the lower
                                       # 45% of the day's range so far (mirrored
                                       # for a PUT). 1.0 disables this filter.
            # --- risk ---
            "sl_pct": 0.30,
            "target_pct": 0.60,
            "lots": 1,
            # --- session windows ---
            "entry_from_hour": 9, "entry_from_minute": 45,
            "entry_to_hour": 14, "entry_to_minute": 0,
            "exit_hour": 15, "exit_minute": 10,
            "max_trades_per_day": 1,
            "warmup_bars": 80,
        }
        self._skew = []            # [(date, skew)] — rebuilt live, see on_bar
        self._day = None
        self._trades_today = 0

    def meta(self) -> StrategyMeta:
        return StrategyMeta(
            name="Skew / Bias Divergence",
            underlying="NIFTY",
            segment="NSE_FNO",
            timeframe="5",
            params=self.params,
            description=("ATM option buy when iv_skew and index_bias disagree. "
                         "BASED ON ONE OBSERVED DAY (2026-08-17) — validate by "
                         "backtest/walk-forward before trusting it."),
        )

    # -- helpers ------------------------------------------------------------

    def _reset_if_new_day(self, ctx):
        d = ctx.now.date()
        if self._day != d:
            self._day = d
            self._skew = []
            self._trades_today = 0

    def _day_range_pos(self, ctx):
        """Where spot sits in TODAY's range so far: 0 = at the low, 1 = high.
        None when the day is too young to have a range worth speaking of."""
        bars = [b for b in ctx.history(90) if b.ts.date() == ctx.now.date()]
        if len(bars) < 3:
            return None
        hi = max(b.high for b in bars)
        lo = min(b.low for b in bars)
        if hi <= lo:
            return None
        return (ctx.spot - lo) / (hi - lo)

    def _signal(self, ctx):
        """'CE', 'PE' or None. Requires BOTH readings; None means unknown and
        unknown never trades."""
        ch = ctx.chain()
        if not ch or ch.get("iv_skew") is None:
            return None
        skew = ch["iv_skew"]
        self._skew.append(skew)
        n = int(self.params["lookback_bars"])
        if len(self._skew) <= n:
            return None                       # not enough history yet
        shift = skew - self._skew[-(n + 1)]

        b = ctx.signal("index_bias")
        if not b or b.get("score") is None:
            return None                       # no breadth read -> no divergence
        score = b["score"]
        gate = self.params["bias_gate"]
        move = self.params["min_skew_shift"]

        # calls richening while breadth is bearish -> side with the surface
        if shift <= -move and score <= -gate:
            return "CE"
        # puts richening while breadth is bullish
        if shift >= move and score >= gate:
            return "PE"
        return None

    # -- main ---------------------------------------------------------------

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        self._reset_if_new_day(ctx)
        p = self.params
        t = ctx.now.time()

        if (t.hour, t.minute) >= (p["exit_hour"], p["exit_minute"]):
            if ctx.positions:
                ctx.exit_all(reason="time_exit")
            return

        side = self._signal(ctx)      # called every bar so _skew stays dense

        # RULE 8: instance state does not survive a restart but positions do.
        # Never enter while anything is open, whatever self._trades_today says.
        if ctx.positions:
            return
        if self._trades_today >= p["max_trades_per_day"]:
            return
        if not ((p["entry_from_hour"], p["entry_from_minute"])
                <= (t.hour, t.minute)
                < (p["entry_to_hour"], p["entry_to_minute"])):
            return
        if side is None:
            return

        # Confirmation: take the turn, not the chase.
        rp = self._day_range_pos(ctx)
        limit = p["max_range_pos"]
        if rp is not None and limit < 1.0:
            if side == "CE" and rp > limit:
                return
            if side == "PE" and rp < (1.0 - limit):
                return

        leg = LegSpec(
            option_type=OptionType.CALL if side == "CE" else OptionType.PUT,
            action=Action.BUY,
            strike_offset=0,
            expiry_kind=ExpiryKind.WEEKLY,
            expiry_offset=0,
            lots=int(p["lots"]),
            tag="divergence",
        )
        if ctx.enter([leg], tag=f"skew_div_{side}",
                     sl_pct=p["sl_pct"], target_pct=p["target_pct"]):
            self._trades_today += 1
            ctx.log(f"{side} on skew/bias divergence — range_pos="
                    f"{'NA' if rp is None else round(rp, 2)}")

    def on_day_end(self, ctx: Context) -> None:
        if ctx.positions:
            ctx.exit_all(reason="time_exit")
