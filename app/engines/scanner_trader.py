"""
Scanner-driven positional paper trader
======================================
The screener finds high-probability stocks; THIS turns those calls into actual
(paper) option-buying trades and manages them positionally — held across days,
with a ratcheting trailing stop, until the setup that justified the trade is
gone.

Why a dedicated engine and not a Strategy subclass: a Strategy is pinned to one
underlying and driven by that underlying's candles. The scanner is inherently
multi-stock and event-driven — it hops between whatever names score highest —
so it doesn't fit the single-underlying contract. Like LiveRunner, this runs
PARALLEL to the Strategy/paper engine and never touches its paths. It DOES reuse
the shared cost model (engines/fills.py) and the same ledger (registry, mode
"PAPER") so results stay comparable (invariant #2).

Everything here is paper-only and gated OFF (`scanner_trade` setting). The
decision logic (sizing, trailing stop, exit) is pure and unit-tested; the async
step just wires it to the live chain cache + ledger.

Trade lifecycle
---------------
ENTRY  a shortlisted setup that passes EVERY entry_quality() gate — score >=
       entry_score AND (by default) a real volume surge, a deep-dived chain
       that passed the liquidity screen, price pressing the day's range in
       the trade's direction, not fighting a strong opposite NIFTY bias, and
       a tight enough bid-ask on the actual contract — while the day-level
       circuit breakers (max_trades_per_day, daily_loss_stop_pct) still
       allow new risk → buy the ATM option of the bias side, sized to risk a
       fixed % of capital. High probability = independent signals agreeing;
       one loud score component can no longer buy an unconfirmed setup.
HOLD   marked to the live chain each cycle; the stop ratchets UP as the premium
       makes new highs (never down).
EXIT   whichever fires first: hard stop, trailing stop, target, max holding
       period, OR the setup decays (score < exit_score) / the bias flips.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone, timedelta

from app.core.contract import Action, Bar
from app.engines import adaptation as A
from app.engines import fills as F
from app.engines import indicators as IND

IST = timezone(timedelta(hours=5, minutes=30))
STRATEGY_ID = "SCANNER"          # ledger id for all scanner-trader rows
BOOK_SETTING = "scanner_trader_book"
CHAL_SETTING = "scanner_challenger"        # active shadow trial (JSON)
PROPOSAL_SETTING = "scanner_proposal"      # trial that won, awaiting the human
TUNE_HISTORY_SETTING = "scanner_tune_history"   # applies/discards/dismissals
EMBARGO_SETTING = "scanner_tune_embargo_until"  # no new trials before this day


@dataclass
class TradeConfig:
    capital: float = 500_000.0
    risk_pct: float = 0.01            # risk 1% of capital per trade
    entry_score: float = 70.0        # min setup score to open (65 pre-2026-08:
                                     # reachable on Tier-1 alone; with the
                                     # liquid-chain gate below, entries carry
                                     # the chain bonus, so the bar moved up)
    exit_score: float = 45.0         # setup decayed below this -> exit
    hard_stop_pct: float = 0.30      # initial stop: 30% below entry premium
    trail_pct: float = 0.25          # trail 25% below the high-water premium
    target_pct: float = 1.00         # optional take-profit (+100%); 0 = off
    max_positions: int = 5
    max_hold_days: int = 10          # positional, but not forever
    max_lots: int = 10
    # -- behavioural gates (defaults preserve pre-existing behaviour exactly;
    # they exist so the adaptation pipeline can TRIAL them — every knob here
    # is reachable by the shadow challenger via replace(cfg, **overrides),
    # so an insight rule mapped in adaptation.ADAPTABLE can prove its value
    # on the virtual book before a human ever applies it) -------------------
    reentry_cooldown_min: float = 0.0  # skip re-entering a symbol within N
                                       # min of its own exit; 0 = off
    entry_cutoff_min: int = 935      # no NEW entries at/after this minute of
                                     # day; >= _NO_ENTRY_CUTOFF_MIN (935) means
                                     # NO cutoff at all, which is the default
    fresh_buildup_only: int = 0      # 1 = only long/short buildup entries
                                     # (skip covering/unwinding-fuelled)
    # -- high-probability entry gates (2026-08-28). The single composite
    # score let one loud component (a big move) buy a setup nothing else
    # confirmed — the book over-traded and bled. These gates demand
    # CONFLUENCE: every independent signal recorded at entry (volume, chain
    # liquidity, intraday structure, market regime) must agree before a rupee
    # is risked, and two day-level circuit breakers cap how much one bad day
    # can cost. Unlike the behavioural gates above these default ON — that IS
    # the change — and each is individually disable-able (0/off) via its
    # scanner_trade_* setting. All are enforced in entry_quality()/
    # pick_entries(), the shared choke point, so the shadow challenger can
    # trial different levels with no extra wiring. ------------------------
    min_volume_surge: float = 1.5    # require >= this x usual volume at entry;
                                     # no baseline = no confirmation = skip
                                     # (0 = off)
    require_liquid_chain: int = 1    # 1 = only names deep-dived THIS Tier-2
                                     # cycle whose chain passed the liquidity
                                     # screen (closes the hole where a Tier-1-
                                     # only score bought an unvetted chain)
    min_range_align: float = 0.6     # LTP must sit this far into the day's
                                     # range in the trade's direction (CE near
                                     # the high, PE near the low); unknown
                                     # range = skip (0 = off)
    index_align: int = 1             # 1 = block entries fighting a STRONG
                                     # opposite NIFTY bias (|score| > 0.3);
                                     # neutral/no reading blocks nothing
    max_trades_per_day: int = 4      # hard cap on NEW entries per day
                                     # (0 = off)
    daily_loss_stop_pct: float = 0.02  # realized day loss >= this fraction of
                                     # capital -> no new entries today; exits/
                                     # management unaffected (0 = off)
    max_entry_spread_pct: float = 2.0  # skip if the actual contract's bid-ask
                                     # spread exceeds this % of mid at fill
                                     # time — or has no two-sided quote at all
                                     # (0 = off)
    # -- technical-read confirmation gates (2026-08-28, the "experienced
    # trader" checklist). Computed per shortlisted/held stock from its own
    # 1-min snapshot series (engines/tech_read.py) and attached to
    # ranked_scores() dicts as sc["tech"]. Same contract as the confluence
    # gates above: defaults ON, each 0/off-able, missing data fails CLOSED
    # (which also embargoes the first ~20 min of a session while EMA/RSI
    # warm up, and a symbol's first recorded day for the pivot R:R). -------
    require_vwap_side: int = 1       # 1 = CE only above session VWAP,
                                     # PE only below (the institutional line)
    require_trend_align: int = 1     # 1 = EMA9 vs EMA21 of the 1-min series
                                     # must agree with the bias
    require_structure_break: int = 1  # 1 = need a confirmed break-AND-HOLD
                                     # of the opening range / prev-day
                                     # high-low in the bias direction
    max_rsi_extreme: float = 75.0    # don't chase: skip CE when RSI(14) >
                                     # this, PE when RSI < (100 - this)
                                     # (0 = off)
    max_vwap_dist_pct: float = 2.5   # don't chase: skip when price sits more
                                     # than this % away from VWAP (0 = off)
    min_rr: float = 1.5              # structural reward:risk floor — next
                                     # pivot target vs the VWAP/level stop
                                     # (0 = off)


@dataclass
class SPosition:
    symbol: str
    bias: str                        # "CE" | "PE"
    side: str                        # "CALL" | "PUT"
    strike: float
    lots: int
    qty_units: int                   # lots * lot_size (long, positive)
    entry_price: float
    entry_fees: float
    entry_ts: str                    # ISO
    entry_score: float
    high_water: float                # highest premium seen (for the trail)
    mtm: float = 0.0
    low_water: float = 0.0           # lowest premium seen (MAE; 0 = unset,
                                     # for books persisted before this field)
    entry_ctx: dict = field(default_factory=dict)   # full setup snapshot at
                                     # entry (score reasons, chain, config) —
                                     # journaled with the exit for analysis

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "SPosition":
        return cls(**d)


# ---------------------------------------------------------------------------
# Pure decision logic
# ---------------------------------------------------------------------------

def size_lots(cfg: TradeConfig, entry_premium: float, lot_size: int) -> int:
    """Lots such that a hard-stop loss ≈ risk_pct of capital. 0 if the trade
    can't be sized (bad premium / lot)."""
    if entry_premium <= 0 or not lot_size or lot_size <= 0:
        return 0
    risk_budget = cfg.capital * cfg.risk_pct
    per_lot_risk = entry_premium * cfg.hard_stop_pct * lot_size
    if per_lot_risk <= 0:
        return 0
    return max(0, min(int(risk_budget // per_lot_risk), cfg.max_lots))


def effective_stop(entry: float, high_water: float, cfg: TradeConfig) -> float:
    """The active stop premium. Until the option trades above entry it's the
    hard floor (`hard_stop_pct` below entry); once it's made a new high in
    profit, the trail (`trail_pct` below the high-water mark) takes over but
    never drops below that hard floor — so the stop only ratchets up."""
    hard = entry * (1 - cfg.hard_stop_pct)
    if high_water <= entry:
        return hard
    return max(hard, high_water * (1 - cfg.trail_pct))


def exit_decision(pos: SPosition, premium: float, score: dict | None,
                  cfg: TradeConfig, held_days: int):
    """(should_exit, reason). Priority: stops/target/time first (capital
    protection), then the setup-based exit."""
    stop = effective_stop(pos.entry_price, pos.high_water, cfg)
    if premium <= stop:
        return True, ("trail_stop" if pos.high_water > pos.entry_price else "hard_stop")
    if cfg.target_pct and premium >= pos.entry_price * (1 + cfg.target_pct):
        return True, "target"
    if held_days >= cfg.max_hold_days:
        return True, "max_hold"
    if score is not None:
        s, b = score.get("score"), score.get("bias")
        if (s is not None and s < cfg.exit_score) or (b and b != pos.bias):
            return True, "setup_gone"
    return False, None


def _minutes_of_day(ts) -> int:
    return ts.hour * 60 + ts.minute


# entry_cutoff_min at/above this is treated as NO cutoff. 935 = 15:35 = the
# NSE close, the default. It must be a true no-op rather than "block after
# 15:35": MCX runs to 23:30, so a live 935 cutoff would silently kill ~8h of
# MCX entries. (Caught by tests run at 16:01 IST — the original test used
# 15:34 and passed by one minute, hiding it.)
_NO_ENTRY_CUTOFF_MIN = 935


def quote_spread_pct(q) -> float | None:
    """Bid-ask spread as % of mid for one option quote; None without a real
    two-sided quote (and an option you can't see both sides of is not one to
    buy under the high-probability profile)."""
    bid, ask = getattr(q, "bid", None), getattr(q, "ask", None)
    if bid and ask and (bid + ask) > 0:
        return round((ask - bid) / ((ask + bid) / 2) * 100.0, 2)
    return None


def entry_quality(sc: dict, cfg: TradeConfig,
                  market_bias: float | None = None) -> tuple[bool, str | None]:
    """The per-candidate entry criteria, in one pure function: (ok, reason).

    A high-probability trade is one where INDEPENDENT signals agree, so each
    gate must pass on its own — a huge score cannot buy back a missing volume
    surge. `sc` is a setup_score() dict off ranked_scores(); `market_bias` is
    the NIFTY index-bias score in [-1, 1] (or None when unread). Confirmation
    gates (surge, liquidity, range, and the whole technical-read block:
    VWAP side, EMA trend, structure break-and-hold, RSI/VWAP-distance
    overextension, structural R:R) treat missing data as a FAIL when armed —
    "unknown" is not confirmation — while the regime gate only blocks on a
    positively contradicting reading. The returned reason is short and
    log-ready so a skipped qualifier is never invisible (the 07-28 lesson)."""
    bias = sc.get("bias")
    if not bias:
        return False, "no bias"
    score = sc.get("score") or 0
    if score < cfg.entry_score:
        return False, f"score {score:g} < {cfg.entry_score:g}"
    # covering/unwinding-fuelled setups when fresh-only is on (unknown
    # buildup stays allowed — this gate is a filter on known-weak fuel)
    if cfg.fresh_buildup_only and sc.get("buildup") in (
            "short_covering", "long_unwinding"):
        return False, f"{sc.get('buildup')} fuel (fresh-buildup-only)"
    if cfg.min_volume_surge:
        surge = sc.get("volume_surge")
        if surge is None:
            return False, "volume surge unknown (no baseline)"
        if surge < cfg.min_volume_surge:
            return False, (f"volume {surge:.1f}x < "
                           f"{cfg.min_volume_surge:g}x required")
    if cfg.require_liquid_chain:
        liq = sc.get("liquidity_ok")
        if liq is False:
            return False, "chain failed the liquidity screen"
        if liq is not True:
            return False, "not deep-dived this cycle (chain unvetted)"
    if cfg.min_range_align:
        rp = sc.get("range_pos")
        aligned = None if rp is None else (rp if bias == "CE" else 1.0 - rp)
        if aligned is None:
            return False, "day-range position unknown"
        if aligned < cfg.min_range_align:
            return False, (f"range align {aligned:.2f} < "
                           f"{cfg.min_range_align:g} (not pressing the "
                           + ("high" if bias == "CE" else "low") + ")")
    if cfg.index_align and market_bias is not None:
        if (bias == "CE" and market_bias < -0.3) or \
                (bias == "PE" and market_bias > 0.3):
            return False, f"{bias} against market bias {market_bias:+.2f}"
    # -- technical-read gates (the experienced-trader checklist). sc["tech"]
    # is technical_read() off the symbol's own 1-min snapshot series; when
    # any of these knobs is armed and the read is missing, fail closed —
    # a trade you can't locate on the chart is not a high-probability trade.
    tech_gates_armed = (cfg.require_vwap_side or cfg.require_trend_align
                        or cfg.require_structure_break or cfg.max_rsi_extreme
                        or cfg.max_vwap_dist_pct or cfg.min_rr)
    if tech_gates_armed:
        tech = sc.get("tech")
        if not tech or not tech.get("n_bars"):
            return False, "no technical read (no snapshot series)"
        if cfg.require_vwap_side:
            vd = tech.get("vwap_dist_pct")
            if vd is None:
                return False, "session VWAP unknown"
            if (bias == "CE" and vd <= 0) or (bias == "PE" and vd >= 0):
                return False, (f"{bias} on the wrong side of VWAP "
                               f"({vd:+.2f}%)")
        if cfg.require_trend_align:
            trend = tech.get("trend")
            if trend is None:
                return False, "EMA trend not established (warming up)"
            if trend != ("up" if bias == "CE" else "down"):
                return False, f"EMA trend {trend} against {bias}"
        if cfg.require_structure_break:
            want_dir = "up" if bias == "CE" else "down"
            if tech.get("structure_break") != want_dir:
                return False, ("no confirmed break-and-hold of OR/prev-day "
                               + ("high" if bias == "CE" else "low"))
        if cfg.max_rsi_extreme:
            rsi = tech.get("rsi")
            if rsi is None:
                return False, "RSI not established (warming up)"
            if bias == "CE" and rsi > cfg.max_rsi_extreme:
                return False, (f"RSI {rsi:.0f} overextended "
                               f"(> {cfg.max_rsi_extreme:g})")
            if bias == "PE" and rsi < (100 - cfg.max_rsi_extreme):
                return False, (f"RSI {rsi:.0f} overextended "
                               f"(< {100 - cfg.max_rsi_extreme:g})")
        if cfg.max_vwap_dist_pct:
            vd = tech.get("vwap_dist_pct")
            if vd is None:
                return False, "session VWAP unknown"
            if abs(vd) > cfg.max_vwap_dist_pct:
                return False, (f"{abs(vd):.1f}% from VWAP — chasing "
                               f"(cap {cfg.max_vwap_dist_pct:g}%)")
        if cfg.min_rr:
            if tech.get("prev_close") is None:
                return False, "no prev-day data for pivots (first recorded day)"
            rr = tech.get("rr_ce" if bias == "CE" else "rr_pe")
            if rr is None:
                return False, "no structural target/stop to measure R:R"
            if rr < cfg.min_rr:
                return False, f"R:R {rr:.1f} < {cfg.min_rr:g}"
    return True, None


def pick_entries(ranked_scores: list, held: set, cfg: TradeConfig,
                 now=None, last_exits: dict | None = None,
                 market_bias: float | None = None,
                 entries_today: int = 0, day_realized: float = 0.0) -> list:
    """Symbols to open this cycle: highest-scoring setups that pass EVERY
    entry_quality() gate, not already held, up to the free-slot count.

    ALL entry gating lives HERE (+ entry_quality above) — the single choke
    point both the champion and the shadow challenger enter through — so any
    TradeConfig knob is trialable by the adaptation pipeline with no extra
    wiring. `now`, `last_exits` ({symbol: exit datetime or ISO str}),
    `market_bias`, `entries_today` (entries already opened today) and
    `day_realized` (today's realized P&L so far) are optional so pure-logic
    callers stay simple."""
    slots = cfg.max_positions - len(held)
    # day-level circuit breakers: a cap on how many NEW trades a day may open,
    # and a realized-loss stop that ends the day's entries outright. Exits and
    # position management are never affected — these only stop new risk.
    if cfg.max_trades_per_day:
        slots = min(slots, cfg.max_trades_per_day - int(entries_today))
    if slots <= 0:
        return []
    if cfg.daily_loss_stop_pct and \
            day_realized <= -abs(cfg.daily_loss_stop_pct) * cfg.capital:
        return []
    # no NEW entries at/after the cutoff (exits/management are unaffected)
    if now is not None and cfg.entry_cutoff_min < _NO_ENTRY_CUTOFF_MIN and \
            _minutes_of_day(now) >= cfg.entry_cutoff_min:
        return []
    out = []
    for sc in ranked_scores:
        sym = sc.get("symbol")
        if not sym or sym in held:
            continue
        ok, _why = entry_quality(sc, cfg, market_bias)
        if not ok:
            continue
        # re-entry cooldown: don't re-buy a symbol within N min of its exit
        if cfg.reentry_cooldown_min and now is not None and last_exits:
            last = last_exits.get(sym)
            if last is not None:
                if isinstance(last, str):
                    try:
                        last = datetime.fromisoformat(last)
                    except ValueError:
                        last = None
                if last is not None and (now - last).total_seconds() / 60.0 \
                        < cfg.reentry_cooldown_min:
                    continue
        out.append(sym)
        if len(out) >= slots:
            break
    return out


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class ScannerTrader:
    def __init__(self, store):
        self.store = store
        self.book: dict[str, SPosition] = {}
        self._fee = F.FeeConfig()
        self._slip = F.SlippageConfig()
        self._prem_hist: dict[tuple[str, str], list] = {}  # (symbol, side) ->
                                     # session's own premium samples (in-memory
                                     # only, for entry_ctx's VWAP/BB distance —
                                     # not used by any trading decision)
        self._noquote: dict[str, str] = {}   # symbol -> day already warned
                                     # about (qualified but unpriceable), so
                                     # the every-cycle entry loop logs once
        self._restore()

    # -- config / persistence ------------------------------------------------
    def _cfg(self) -> TradeConfig:
        from app.core import registry

        def _f(key, default):
            try:
                return float(registry.setting(key, str(default)))
            except (TypeError, ValueError):
                return default

        return TradeConfig(
            capital=_f("scanner_trade_capital", 500_000.0),
            risk_pct=_f("scanner_trade_risk_pct", 0.01),
            entry_score=_f("scanner_trade_entry_score", 70.0),
            exit_score=_f("scanner_trade_exit_score", 45.0),
            hard_stop_pct=_f("scanner_trade_hard_stop_pct", 0.30),
            trail_pct=_f("scanner_trade_trail_pct", 0.25),
            target_pct=_f("scanner_trade_target_pct", 1.00),
            max_positions=int(_f("scanner_trade_max_positions", 5)),
            max_hold_days=int(_f("scanner_trade_max_hold_days", 10)),
            max_lots=int(_f("scanner_trade_max_lots", 10)),
            reentry_cooldown_min=_f("scanner_trade_reentry_cooldown_min", 0.0),
            entry_cutoff_min=int(_f("scanner_trade_entry_cutoff_min", 935)),
            fresh_buildup_only=int(_f("scanner_trade_fresh_buildup_only", 0)),
            min_volume_surge=_f("scanner_trade_min_volume_surge", 1.5),
            require_liquid_chain=int(
                _f("scanner_trade_require_liquid_chain", 1)),
            min_range_align=_f("scanner_trade_min_range_align", 0.6),
            index_align=int(_f("scanner_trade_index_align", 1)),
            max_trades_per_day=int(_f("scanner_trade_max_trades_per_day", 4)),
            daily_loss_stop_pct=_f("scanner_trade_daily_loss_stop_pct", 0.02),
            max_entry_spread_pct=_f("scanner_trade_max_entry_spread_pct", 2.0),
            require_vwap_side=int(_f("scanner_trade_require_vwap_side", 1)),
            require_trend_align=int(
                _f("scanner_trade_require_trend_align", 1)),
            require_structure_break=int(
                _f("scanner_trade_require_structure_break", 1)),
            max_rsi_extreme=_f("scanner_trade_max_rsi_extreme", 75.0),
            max_vwap_dist_pct=_f("scanner_trade_max_vwap_dist_pct", 2.5),
            min_rr=_f("scanner_trade_min_rr", 1.5),
        )

    def _persist(self) -> None:
        from app.core import registry
        try:
            registry.set_setting(
                BOOK_SETTING,
                json.dumps({s: p.to_json() for s, p in self.book.items()}))
        except Exception:
            pass

    def _restore(self) -> None:
        from app.core import registry
        try:
            raw = registry.setting(BOOK_SETTING, "")
            if raw:
                self.book = {s: SPosition.from_json(d)
                             for s, d in json.loads(raw).items()}
        except Exception:
            self.book = {}

    def held_symbols(self) -> list[str]:
        """Symbols with an open position — the scanner must keep polling their
        chains even after they leave the shortlist, so MTM/exits stay live."""
        return list(self.book)

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _side_for(bias: str) -> str:
        return "CALL" if bias == "CE" else "PUT"

    def _atm_quote(self, hub, symbol: str, side: str):
        cache = hub._chain_cache.get(symbol) or {}
        for (kind, off, soff, otype), q in cache.items():
            if soff == 0 and otype == side:
                return q
        return None

    def _lot_size(self, scanner, symbol: str) -> int:
        u = scanner._universe.get(symbol) or {}
        return int(u.get("lot_size") or 0)

    @staticmethod
    def _market_bias(scanner) -> float | None:
        """The scanner's live NIFTY index-bias score in [-1, 1] — the broad-
        market regime read the index_align gate judges stock entries against.
        None when unread (scanner warming up / bias not computed)."""
        reading = (getattr(scanner, "index_bias", None) or {}).get("NIFTY")
        return (reading or {}).get("score")

    def _day_entry_state(self, day) -> tuple[int, float]:
        """(entries opened today, today's realized P&L) feeding the day-level
        circuit breakers. Both come from PERSISTED state (journal / daily_pnl)
        rather than in-memory counters, so a mid-day restart can't reset the
        trades-per-day cap or the loss stop. Best-effort: on any read failure
        the gates see (0, 0.0) — i.e. they fail OPEN, never wedge trading on
        a broken read."""
        from app.core import registry
        prefix = day.isoformat()
        entries = 0
        try:
            entries = sum(
                1 for r in registry.journal_rows(limit=300, kind="entry")
                if (r.get("ts") or "").startswith(prefix))
        except Exception:
            pass
        realized = 0.0
        try:
            row = next((r for r in registry.performance_rows(STRATEGY_ID, "PAPER")
                        if r["trade_date"] == prefix), None)
            realized = (row["realized"] if row else 0.0) or 0.0
        except Exception:
            pass
        return entries, realized

    def _log_entry_halt(self, cfg: TradeConfig, day,
                        entries_today: int, day_realized: float) -> None:
        """One visible event per day when a day-level circuit breaker engages
        — an entry-less afternoon must be readable as 'halted, working as
        designed', not as a scanner failure."""
        from app.core import registry
        halt = None
        if cfg.daily_loss_stop_pct and \
                day_realized <= -abs(cfg.daily_loss_stop_pct) * cfg.capital:
            halt = ("daily-loss stop: realized "
                    f"₹{round(day_realized)} breaches "
                    f"{cfg.daily_loss_stop_pct:.1%} of capital — no new "
                    "entries today (exits still managed)")
        elif cfg.max_trades_per_day and \
                entries_today >= cfg.max_trades_per_day:
            halt = (f"trade cap: {entries_today} entries today reached "
                    f"max_trades_per_day={cfg.max_trades_per_day} — no new "
                    "entries today")
        if not halt:
            return
        marker = (day.isoformat(), halt[:10])
        if getattr(self, "_halt_logged", None) != marker:
            self._halt_logged = marker
            registry.record_event("info", "scanner", f"entry halt — {halt}")

    def _log_gated(self, sym: str, now, detail: str) -> None:
        """Once per (symbol, day, reason): why a qualifying setup was NOT
        entered. Same discipline as _noquote — the loop runs every cycle, the
        log must not flood, but a skip must never be silent."""
        from app.core import registry
        gated = getattr(self, "_gated", None)
        if gated is None:
            gated = self._gated = {}
        key = f"{now.date().isoformat()}:{detail}"
        if gated.get(sym) != key:
            gated[sym] = key
            registry.record_event("info", "scanner",
                                  f"entry gated [{sym}]: {detail}")

    def _sample_candidates(self, hub, scanner, now) -> int:
        """Record the ATM premium of every scored candidate (any symbol the
        scanner ranked with a bias) into its rolling window, whether or not we
        trade it. Purely observational — feeds entry_ctx's VWAP/%B so those
        reflect the option's own recent path instead of only existing for
        symbols we already held. Cache-only, no API calls; never raises."""
        n = 0
        try:
            for sc in scanner.ranked_scores():
                sym, bias = sc.get("symbol"), sc.get("bias")
                if not sym or not bias:
                    continue
                side = self._side_for(bias)
                q = self._atm_quote(hub, sym, side)
                if q is not None and q.ltp:
                    self._sample_premium(sym, side, now, q.ltp)
                    n += 1
        except Exception:
            pass          # observation must never disturb trading
        return n

    def _sample_premium(self, sym: str, side: str, ts, ltp) -> None:
        """Append this cycle's option LTP to the (symbol, side)'s in-session
        rolling window, feeding VWAP/Bollinger for entry_ctx. Cleared across a
        date change so a restart or an overnight hold never mixes sessions."""
        if not ltp:
            return
        store = getattr(self, "_prem_hist", None)
        if store is None:
            store = self._prem_hist = {}
        hist = store.setdefault((sym, side), [])
        if hist and hist[-1].ts.date() != ts.date():
            hist.clear()
        hist.append(Bar(ts=ts, open=ltp, high=ltp, low=ltp, close=ltp))
        if len(hist) > 60:
            del hist[:-60]

    # -- the step ------------------------------------------------------------
    def manage(self, hub, scanner) -> set[str]:
        """Mark every open position to its live premium and exit whichever
        fires a stop/target/score-decay/max-hold rule, then book the day's
        P&L. Pure position upkeep — no shortlist/discovery, no new entries —
        so unlike the full step() (which needs a freshly re-ranked shortlist
        and only runs once per ~5-min Tier-2 cycle), this is safe and cheap to
        call far more often. scanner.py's run_position_mtm() does exactly
        that for just the held symbols, so today's P&L and stop/target checks
        stay close to real time between full Tier-2 cycles instead of lagging
        up to TIER2_INTERVAL behind. Returns the set of symbols exited this
        call. No-op (returns empty) unless `scanner_trade` is on."""
        from app.core import registry
        if registry.setting("scanner_trade", "off") != "on":
            return set()
        cfg = self._cfg()
        now = datetime.now(IST).replace(tzinfo=None)
        day = now.date()
        realized_today = 0.0
        fees_today = 0.0

        exited: set[str] = set()
        for sym, pos in list(self.book.items()):
            q = self._atm_quote(hub, sym, pos.side)
            if q is None or not q.ltp:
                continue                               # no live quote -> hold
            premium = q.ltp
            self._sample_premium(sym, pos.side, now, premium)
            pos.mtm = premium
            pos.high_water = max(pos.high_water, premium)
            pos.low_water = min(pos.low_water or premium, premium)
            held_days = (day - datetime.fromisoformat(pos.entry_ts).date()).days
            do_exit, reason = exit_decision(
                pos, premium, scanner.scores.get(sym), cfg, held_days)
            if not do_exit:
                continue
            fill = F.fill_live(q, Action.SELL, pos.qty_units, self._fee, self._slip)
            realized = ((fill.price - pos.entry_price) * pos.qty_units
                        - pos.entry_fees - fill.fees)
            realized_today += realized
            fees_today += fill.fees
            self._book_trade(sym, pos, "exit", fill.price, fill.fees, reason, now,
                             realized=realized)
            self._journal_exit(sym, pos, fill, reason, realized, scanner, now,
                              held_days)
            registry.record_event(
                "info", "scanner",
                f"trade EXIT {sym} {pos.bias} @ {fill.price} ({reason}) "
                f"P&L ₹{round(realized)}")
            del self.book[sym]
            exited.add(sym)
            # feed the re-entry cooldown gate (in-memory: a restart forgets
            # these, so the worst case is one early re-entry after a restart
            # — bounded by the cooldown length itself)
            store = getattr(self, "_last_exit", None)
            if store is None:
                store = self._last_exit = {}
            store[sym] = now

        # a closed trade is new evidence — reflect on the journal (at most
        # once a day) and surface any data-backed suggestion as an event
        if exited:
            self._daily_reflection(cfg, day)

        self._book_day(cfg, day, realized_today, fees_today)
        return exited

    def _book_day(self, cfg: TradeConfig, day, realized_delta: float,
                  fees_delta: float) -> None:
        """Accumulate this call's fresh realized/fees onto today's daily_pnl
        row and persist the open book. daily_pnl is a single row per
        (strategy, mode, date) that a save REPLACES wholesale — so, since
        manage() may now be called many times a day (the fast loop) as well
        as once per Tier-2 cycle, we must read today's existing row back and
        add this call's delta onto it, never overwrite it with just this
        call's delta. Reading from the DB (rather than tracking a running
        total in memory) means the day's total is correct even across a
        mid-day restart, and — since a new calendar date simply has no row
        yet — the day's total starts fresh at midnight with no separate
        reset logic needed."""
        from app.core import registry
        unrealized = sum((p.mtm - p.entry_price) * p.qty_units
                         for p in self.book.values())
        if realized_delta or fees_delta or self.book:
            today_iso = day.isoformat()
            today_row = next((r for r in registry.performance_rows(STRATEGY_ID, "PAPER")
                              if r["trade_date"] == today_iso), None)
            realized_day_total = (today_row["realized"] if today_row else 0.0) or 0.0
            realized_day_total += realized_delta
            fees_day_total = (today_row["fees"] if today_row else 0.0) or 0.0
            fees_day_total += fees_delta
            cum = registry.cum_pnl(STRATEGY_ID) + realized_delta
            equity = cfg.capital + cum + unrealized
            registry.save_paper_day(STRATEGY_ID, today_iso,
                                    realized_day_total, unrealized, fees_day_total, equity)
        self._persist()

    def step(self, hub, scanner) -> None:
        """Full cycle: manage() (mark/exit, see above) then open new positions
        from the freshest ranked setups. New entries need a fresh shortlist/
        score, so — unlike manage() — this can't run any faster than the
        Tier-2 cycle that computed them (~every TIER2_INTERVAL). No-op unless
        `scanner_trade` is on."""
        from app.core import registry
        if registry.setting("scanner_trade", "off") != "on":
            return
        cfg = self._cfg()
        now = datetime.now(IST).replace(tzinfo=None)
        day = now.date()
        exited = self.manage(hub, scanner)

        # open new positions from the freshest ranked setups. Names exited
        # THIS cycle are held out so a trailing-stop exit can't immediately
        # re-buy the same name on the still-elevated score (churn).
        # Sample EVERY candidate's premium, not just the ones we enter. This is
        # what removes the selection bias in entry_ctx's VWAP/%B: previously a
        # symbol was only sampled once held or at the instant of entry, so a
        # first entry always had 1 sample (VWAP == price) and only RE-entries
        # carried real history — making the populated rows a proxy for churn
        # rather than a measure of entry quality. Reads the chain cache Tier-2
        # already populated, so it costs no extra API calls.
        self._sample_candidates(hub, scanner, now)

        held = set(self.book) | exited
        fees_today = 0.0
        # One fresh snapshot for BOTH gating and booking: pick_entries decides
        # on this list, so bias/entry_score must come from the same dicts —
        # scanner.scores is the per-Tier-2-cycle copy (up to ~7 min stale) and
        # for a fresh qualifier not in the last shortlist it's absent entirely,
        # which used to null the bias and silently kill the entry.
        ranked = scanner.ranked_scores()
        by_sym = {r.get("symbol"): r for r in ranked}
        market_bias = self._market_bias(scanner)
        entries_today, day_realized = self._day_entry_state(day)
        self._log_entry_halt(cfg, day, entries_today, day_realized)
        picks = pick_entries(ranked, held, cfg,
                             now=now,
                             last_exits=getattr(self, "_last_exit", None),
                             market_bias=market_bias,
                             entries_today=entries_today,
                             day_realized=day_realized)
        # Visibility for the quality gates: a name clearing the score bar but
        # failing a gate is logged once per (symbol, reason) per day. Without
        # this, a gated qualifier looks exactly like a quiet market — the same
        # silence that hid the 07-28 no-quote drops.
        for sc in ranked:
            g_sym = sc.get("symbol")
            if not g_sym or g_sym in held or g_sym in picks \
                    or not sc.get("bias") \
                    or (sc.get("score") or 0) < cfg.entry_score:
                continue
            ok, why = entry_quality(sc, cfg, market_bias)
            if not ok and why:
                self._log_gated(g_sym, now,
                                f"score {sc.get('score')} qualifies but {why}")
        for sym in picks:
            sc = by_sym.get(sym) or scanner.scores.get(sym) or {}
            side = self._side_for(sc.get("bias"))
            q = self._atm_quote(hub, sym, side)
            if q is None or not (q.ask or q.ltp):
                # A qualifying setup we CANNOT price. This used to `continue`
                # in silence, which is how 2026-07-28 looked identical to "no
                # setup qualified": three names cleared entry_score, none had
                # been deep-dived, all three were dropped without a trace.
                # Once per symbol per day — this loop runs every cycle and the
                # log must not flood. (The shadow challenger's copy of this
                # branch stays silent: same cause, twice the noise.)
                day = now.date().isoformat()
                if self._noquote.get(sym) != day:
                    self._noquote[sym] = day
                    registry.record_event(
                        "warn", "scanner",
                        f"entry skipped [{sym}]: qualified (score="
                        f"{sc.get('score')}) but no chain quote — not "
                        f"deep-dived this cycle")
                continue
            # the actual contract must be exitable: cap the bid-ask spread at
            # the moment of fill (the chain-level screen vetted near-ATM
            # strikes minutes ago; this vets THIS contract NOW)
            spread = quote_spread_pct(q)
            if cfg.max_entry_spread_pct and \
                    (spread is None or spread > cfg.max_entry_spread_pct):
                self._log_gated(
                    sym, now,
                    "spread " + ("unknown (one-sided quote)" if spread is None
                                 else f"{spread:.1f}%") +
                    f" exceeds the {cfg.max_entry_spread_pct:g}% entry cap")
                continue
            # (no _sample_premium here — _sample_candidates() above already
            # sampled every ranked candidate this cycle; sampling again would
            # double-count this timestamp and skew the window toward entries)
            lot_size = self._lot_size(scanner, sym)
            probe = F.fill_live(q, Action.BUY, lot_size or 1, self._fee, self._slip)
            lots = size_lots(cfg, probe.price, lot_size)
            if lots <= 0:
                continue
            qty = lots * lot_size
            fill = F.fill_live(q, Action.BUY, qty, self._fee, self._slip)
            pos = SPosition(
                symbol=sym, bias=sc.get("bias"), side=side, strike=q.strike,
                lots=lots, qty_units=qty, entry_price=fill.price,
                entry_fees=fill.fees, entry_ts=now.isoformat(),
                entry_score=sc.get("score") or 0.0, high_water=fill.price,
                mtm=fill.price, low_water=fill.price,
                entry_ctx=self._entry_context(scanner, sym, sc, q, cfg))
            self.book[sym] = pos
            fees_today += fill.fees
            self._book_trade(sym, pos, "entry", fill.price, fill.fees, "entry", now)
            self._journal_entry(sym, pos, q, now)
            registry.record_event(
                "info", "scanner",
                f"trade ENTRY {sym} {pos.bias} x{lots} @ {fill.price} "
                f"(score {pos.entry_score})")
            held.add(sym)

        # step the shadow challenger (if a config trial is running) on the
        # same scores/quotes — virtual book, never touches the ledger
        try:
            self._step_challenger(hub, scanner, now, day)
        except Exception:
            pass

        # book the entries' fees too (manage() already booked the exits above)
        self._book_day(cfg, day, 0.0, fees_today)

    # -- journal (rich per-trade log for strategy improvement) ---------------
    def _entry_context(self, scanner, sym: str, sc: dict, q, cfg: TradeConfig) -> dict:
        """Everything known about the setup at the moment of entry — Tier-1
        read, Tier-2 chain state, the option's own quote, and the config that
        sized the trade. Journaled now and again with the exit, so every
        closed trade is a self-contained record for later analysis."""
        t1 = (getattr(scanner, "metrics", None) or {}).get(sym) or {}
        t2 = (getattr(scanner, "tier2", None) or {}).get(sym) or {}
        spread_pct = quote_spread_pct(q)
        # How "cheap" is this premium relative to its OWN session so far —
        # we only ever buy premium (CE or PE), so a good entry is one near
        # the option's own VWAP / lower Bollinger band, same test both ways.
        # Sample count is recorded so thin windows are FILTERABLE rather than
        # silently misleading. Previously a first-ever entry had exactly one
        # sample, so VWAP == the entry price and the field read 0.00 — which
        # looks like "entered exactly at VWAP" but means "no history". In the
        # 07-24 data every populated row was a RE-entry and every 0.00 row was
        # a first entry, so the populated rows were ~70% INFY churn (the day's
        # worst losses): bucketing that would have "proven" a spurious result.
        hist = (getattr(self, "_prem_hist", None) or {}).get(
            (sym, self._side_for(sc.get("bias"))), [])
        n_samples = len(hist)
        opt_ltp = q.ltp
        # need >= 2 samples for VWAP to mean anything other than "the price"
        vwap_val = IND.vwap(hist) if n_samples >= 2 else None
        opt_dist_to_vwap_pct = (
            round((opt_ltp - vwap_val) / vwap_val * 100, 2)
            if vwap_val and opt_ltp else None)
        # %B (position WITHIN the band, 0 = at lower, 1 = at upper) rather
        # than distance-to-lower-band: the old metric conflated position with
        # band WIDTH, so the same number meant different things on a quiet vs
        # volatile name and wasn't comparable across symbols.
        bb = IND.bollinger(hist, n=min(20, n_samples)) if n_samples >= 5 else None
        opt_pct_b = None
        if bb and opt_ltp is not None:
            span = (bb["upper"] or 0) - (bb["lower"] or 0)
            if span > 0:
                opt_pct_b = round((opt_ltp - bb["lower"]) / span, 3)
        return {
            "score": sc.get("score"), "reasons": sc.get("reasons") or [],
            "buildup": sc.get("buildup") or t1.get("buildup"),
            "spot": t1.get("spot"), "fut_ltp": t1.get("ltp"),
            "price_change_pct": t1.get("price_change_pct"),
            "oi_change_pct": t1.get("oi_change_pct"),
            "volume_surge": t1.get("volume_surge"),
            "range_pos": t1.get("range_pos"),
            "pcr_oi": t2.get("pcr_oi"), "atm_iv": t2.get("atm_iv"),
            "iv_skew": t2.get("iv_skew"),
            # market regime at entry — lets the journal prove/disprove the
            # counter-trend hypothesis (rule: counter_trend_entries)
            "market_bias": self._market_bias(scanner),
            # the full technical read the entry was judged on (VWAP/EMA/RSI/
            # structure/R:R) — self-contained evidence for future insight
            # rules (e.g. low_rr_entries)
            "tech": sc.get("tech"),
            "worst_spread_pct": (t2.get("liquidity") or {}).get("worst_spread_pct"),
            "opt_bid": q.bid, "opt_ask": q.ask, "opt_ltp": q.ltp,
            "opt_iv": getattr(q, "iv", None), "opt_oi": q.oi,
            "opt_spread_pct": spread_pct,
            "opt_dist_to_vwap_pct": opt_dist_to_vwap_pct,
            "opt_pct_b": opt_pct_b,
            "opt_prem_samples": n_samples,
            "expiry": str(q.expiry) if getattr(q, "expiry", None) else None,
            "config": {"entry_score": cfg.entry_score,
                       "exit_score": cfg.exit_score,
                       "hard_stop_pct": cfg.hard_stop_pct,
                       "trail_pct": cfg.trail_pct,
                       "target_pct": cfg.target_pct,
                       "risk_pct": cfg.risk_pct,
                       "min_volume_surge": cfg.min_volume_surge,
                       "require_liquid_chain": cfg.require_liquid_chain,
                       "min_range_align": cfg.min_range_align,
                       "index_align": cfg.index_align,
                       "max_trades_per_day": cfg.max_trades_per_day,
                       "daily_loss_stop_pct": cfg.daily_loss_stop_pct,
                       "max_entry_spread_pct": cfg.max_entry_spread_pct,
                       "require_vwap_side": cfg.require_vwap_side,
                       "require_trend_align": cfg.require_trend_align,
                       "require_structure_break": cfg.require_structure_break,
                       "max_rsi_extreme": cfg.max_rsi_extreme,
                       "max_vwap_dist_pct": cfg.max_vwap_dist_pct,
                       "min_rr": cfg.min_rr},
        }

    def _journal_entry(self, sym: str, pos: SPosition, q, now) -> None:
        from app.core import registry
        try:
            registry.record_journal(sym, "entry", {
                "bias": pos.bias, "side": pos.side, "strike": pos.strike,
                "lots": pos.lots, "qty_units": pos.qty_units,
                "entry_price": pos.entry_price, "entry_fees": pos.entry_fees,
                "quote_ltp": q.ltp,   # slippage = entry_price - quote_ltp
                "entry_score": pos.entry_score,
                "entry_ctx": pos.entry_ctx,
            }, ts=now.isoformat())
        except Exception:
            pass                      # journaling must never block a trade

    def _journal_exit(self, sym: str, pos: SPosition, fill, reason: str,
                      realized: float, scanner, now, held_days: int) -> None:
        """One self-contained round-trip record: entry context + exit facts +
        the excursion stats (MFE/MAE) that entry/stop tuning feeds on."""
        from app.core import registry
        try:
            entry = pos.entry_price
            lw = pos.low_water or entry
            held_min = None
            try:
                held_min = int((now - datetime.fromisoformat(pos.entry_ts))
                               .total_seconds() // 60)
            except (ValueError, TypeError):
                pass
            sc_now = (getattr(scanner, "scores", None) or {}).get(sym) or {}
            t1_now = (getattr(scanner, "metrics", None) or {}).get(sym) or {}
            registry.record_journal(sym, "exit", {
                "bias": pos.bias, "side": pos.side, "strike": pos.strike,
                "lots": pos.lots, "qty_units": pos.qty_units,
                "entry_ts": pos.entry_ts, "entry_price": entry,
                "entry_fees": pos.entry_fees, "entry_score": pos.entry_score,
                "exit_price": fill.price, "exit_fees": fill.fees,
                "reason": reason, "realized": round(realized, 2),
                "ret_pct": round((fill.price - entry) / entry * 100, 2)
                if entry else None,
                "high_water": pos.high_water, "low_water": lw,
                "mfe_pct": round((pos.high_water - entry) / entry * 100, 2)
                if entry else None,
                "mae_pct": round((entry - lw) / entry * 100, 2)
                if entry else None,
                "held_minutes": held_min, "held_days": held_days,
                "exit_score": sc_now.get("score"),
                "exit_bias": sc_now.get("bias"),
                "exit_spot": t1_now.get("spot"),
                "entry_ctx": pos.entry_ctx,
            }, ts=now.isoformat())
        except Exception:
            pass

    def reflect(self) -> dict:
        """Analyze every closed trade in the journal -> stats + suggestions.
        Backs GET /scanner/insights."""
        from app.core import registry
        from app.engines import journal_insights
        cfg = self._cfg()
        exits = registry.journal_rows(limit=2000, kind="exit")
        return journal_insights.analyze(exits, config={
            "entry_score": cfg.entry_score, "exit_score": cfg.exit_score,
            "hard_stop_pct": cfg.hard_stop_pct, "trail_pct": cfg.trail_pct,
            "target_pct": cfg.target_pct, "risk_pct": cfg.risk_pct})

    def _daily_reflection(self, cfg: TradeConfig, day) -> None:
        """At most once per day, after a close: surface data-backed insights as
        events, then advance the adaptation pipeline (persistence -> shadow
        trial -> proposal). Insights and trials PROPOSE; only apply_proposal(),
        behind the human's click, ever changes a setting."""
        from app.core import registry
        try:
            if registry.setting("scanner_insight_day", "") == day.isoformat():
                return
            registry.set_setting("scanner_insight_day", day.isoformat())
            res = self.reflect()
            real = [s for s in (res.get("suggestions") or [])
                    if s.get("rule") != "insufficient_data"]
            for s in real[:2]:        # at most two, most-supported first
                registry.record_event(
                    "info", "insight",
                    f"journal insight: {s['suggestion']} ({s['evidence']})")
            self._advance_adaptation(cfg, day, real)
        except Exception:
            pass

    # -- adaptation: persistence -> shadow trial -> proposal -> human apply --
    def _tune_history(self) -> list[dict]:
        from app.core import registry
        try:
            return json.loads(registry.setting(TUNE_HISTORY_SETTING, "") or "[]")
        except (TypeError, ValueError):
            return []

    def _save_tune_history(self, hist: list[dict]) -> None:
        from app.core import registry
        registry.set_setting(TUNE_HISTORY_SETTING, json.dumps(hist[-50:]))

    def _advance_adaptation(self, cfg: TradeConfig, day, fired: list[dict]) -> None:
        """One daily tick of the pipeline. Every gate that fails simply waits —
        nothing here ever mutates trading settings."""
        from app.core import registry
        registry.record_insight_rules("scanner", day.isoformat(), fired)

        # 1) measure the last APPLIED change once its post-sample matures;
        # surface (never auto-revert) if it made things worse
        hist = self._tune_history()
        applied = [h for h in hist if h.get("kind") == "apply"]
        if applied and not applied[-1].get("verdict"):
            exits = registry.journal_rows(limit=2000, kind="exit")
            m = A.measure_applied(exits, applied[-1].get("ts") or "")
            if m["ready"]:
                applied[-1]["verdict"] = m["verdict"]
                self._save_tune_history(hist)
                if m["verdict"] == "worse":
                    registry.record_event(
                        "warn", "insight",
                        f"adaptive change {applied[-1].get('rule')} is "
                        f"underperforming its baseline "
                        f"(post {m['post']['expectancy']}/trade over "
                        f"{m['post']['n']} vs pre {m['pre']['expectancy']}) — "
                        "consider reverting it in trade settings")

        # 2) a proposal is already waiting on the human — nothing else to do
        if registry.setting(PROPOSAL_SETTING, ""):
            return

        # 3) an active shadow trial: decide it when mature, else keep running
        raw = registry.setting(CHAL_SETTING, "")
        if raw:
            try:
                st = json.loads(raw)
            except (TypeError, ValueError):
                registry.set_setting(CHAL_SETTING, "")
                return
            started = st.get("started") or day.isoformat()
            days_run = (day - date.fromisoformat(started[:10])).days
            champ = [r for r in registry.journal_rows(limit=2000, kind="exit")
                     if (r.get("ts") or "") >= started]
            cmp = A.compare_books(champ, st.get("closed") or [])
            if days_run >= A.MIN_TRIAL_DAYS and cmp["ready"]:
                registry.set_setting(CHAL_SETTING, "")
                if cmp["better"]:
                    spec = A.ADAPTABLE.get(st.get("rule"), {})
                    proposal = {
                        "rule": st.get("rule"),
                        "overrides": st.get("overrides") or {},
                        "current": {k: getattr(cfg, k, None)
                                    for k in (st.get("overrides") or {})},
                        "suggestion": (f"Shadow trial says: "
                                       f"{spec.get('label', st.get('rule'))} "
                                       f"to {st.get('overrides')}"),
                        "comparison": cmp,
                        "started": started, "created": day.isoformat(),
                    }
                    registry.set_setting(PROPOSAL_SETTING, json.dumps(proposal))
                    registry.record_event(
                        "info", "insight",
                        f"ADAPTIVE UPDATE READY: {proposal['suggestion']} — "
                        f"challenger ₹{cmp['challenger']['expectancy']}/trade "
                        f"({cmp['challenger']['n']}) vs current "
                        f"₹{cmp['champion']['expectancy']}/trade "
                        f"({cmp['champion']['n']}) over the same "
                        f"{days_run} days. Review it on the Scanner page.")
                else:
                    hist.append({"kind": "discard", "rule": st.get("rule"),
                                 "ts": day.isoformat(), "comparison": cmp})
                    self._save_tune_history(hist)
                    registry.record_event(
                        "info", "insight",
                        f"shadow trial {st.get('rule')} did not beat the "
                        f"current config — discarded "
                        f"(challenger ₹{cmp['challenger']['expectancy']} vs "
                        f"champion ₹{cmp['champion']['expectancy']}/trade)")
            elif days_run >= A.MAX_TRIAL_DAYS:
                registry.set_setting(CHAL_SETTING, "")
                hist.append({"kind": "discard", "rule": st.get("rule"),
                             "ts": day.isoformat(), "reason": "inconclusive"})
                self._save_tune_history(hist)
                registry.record_event(
                    "info", "insight",
                    f"shadow trial {st.get('rule')} inconclusive after "
                    f"{days_run} days (too few trades) — discarded")
            return

        # 4) no trial running: start one only past the embargo, for a rule
        # that keeps firing, hasn't been tried recently, and has a step left
        embargo = registry.setting(EMBARGO_SETTING, "")
        if embargo and day.isoformat() < embargo:
            return
        since = (day - timedelta(days=A.PERSIST_WINDOW_DAYS)).isoformat()
        history = registry.insight_history_rows("scanner", since)
        blocked = {h.get("rule") for h in hist
                   if (h.get("ts") or "") >= (day - timedelta(
                       days=A.RULE_COOLDOWN_DAYS)).isoformat()}
        for rule in A.persistent_rules(history):
            if rule in blocked:
                continue
            overrides = A.challenger_overrides(asdict(cfg), rule)
            if not overrides:
                continue
            registry.set_setting(CHAL_SETTING, json.dumps({
                "rule": rule, "overrides": overrides,
                "started": day.isoformat(), "book": {}, "closed": []}))
            registry.record_event(
                "info", "insight",
                f"insight '{rule}' persisted {A.MIN_PERSIST_DAYS}+ days — "
                f"starting a shadow trial of {overrides} alongside the "
                f"current config (no settings changed)")
            break

    def _step_challenger(self, hub, scanner, now, day) -> None:
        """Run the challenger config's virtual book one cycle on the SAME
        scores and quotes the champion just traded. No ledger, no journal, no
        events — its closed trades accumulate in its own state until the trial
        is decided."""
        from app.core import registry
        raw = registry.setting(CHAL_SETTING, "")
        if not raw:
            return
        try:
            st = json.loads(raw)
        except (TypeError, ValueError):
            return
        chal_cfg = replace(self._cfg(), **(st.get("overrides") or {}))
        book = {s: SPosition.from_json(d)
                for s, d in (st.get("book") or {}).items()}
        closed = st.get("closed") or []
        exited: set[str] = set()
        for sym, pos in list(book.items()):
            q = self._atm_quote(hub, sym, pos.side)
            if q is None or not q.ltp:
                continue
            premium = q.ltp
            pos.mtm = premium
            pos.high_water = max(pos.high_water, premium)
            pos.low_water = min(pos.low_water or premium, premium)
            held_days = (day - datetime.fromisoformat(pos.entry_ts).date()).days
            do_exit, reason = exit_decision(
                pos, premium, scanner.scores.get(sym), chal_cfg, held_days)
            if not do_exit:
                continue
            fill = F.fill_live(q, Action.SELL, pos.qty_units,
                               self._fee, self._slip)
            realized = ((fill.price - pos.entry_price) * pos.qty_units
                        - pos.entry_fees - fill.fees)
            closed.append({"symbol": sym, "realized": round(realized, 2),
                           "reason": reason, "entry_ts": pos.entry_ts,
                           "ts": now.isoformat()})
            del book[sym]
            exited.add(sym)
        held = set(book) | exited
        # the challenger's OWN exit times (not the champion's) feed its
        # cooldown gate — derived from its persisted closed-trades list so a
        # cooldown trial survives restarts, unlike the champion's in-memory map
        chal_last_exit: dict = {}
        for c in closed:
            s, ts = c.get("symbol"), c.get("ts")
            if s and ts and ts > chal_last_exit.get(s, ""):
                chal_last_exit[s] = ts
        chal_ranked = scanner.ranked_scores()
        chal_by_sym = {r.get("symbol"): r for r in chal_ranked}
        # the challenger's own day-state (its virtual entries/realized today,
        # not the champion's) feeds its day-level circuit breakers, so a
        # trial of max_trades_per_day / daily_loss_stop_pct is honest
        day_prefix = day.isoformat()
        chal_entries_today = (
            sum(1 for c in closed
                if (c.get("entry_ts") or "").startswith(day_prefix))
            + sum(1 for p in book.values()
                  if (p.entry_ts or "").startswith(day_prefix)))
        chal_day_realized = sum(
            c.get("realized") or 0 for c in closed
            if (c.get("ts") or "").startswith(day_prefix))
        for sym in pick_entries(chal_ranked, held, chal_cfg,
                                now=now, last_exits=chal_last_exit,
                                market_bias=self._market_bias(scanner),
                                entries_today=chal_entries_today,
                                day_realized=chal_day_realized):
            sc = chal_by_sym.get(sym) or scanner.scores.get(sym) or {}
            side = self._side_for(sc.get("bias"))
            q = self._atm_quote(hub, sym, side)
            if q is None or not (q.ask or q.ltp):
                continue
            spread = quote_spread_pct(q)
            if chal_cfg.max_entry_spread_pct and \
                    (spread is None or spread > chal_cfg.max_entry_spread_pct):
                continue                      # same fill-time gate, silent
            lot_size = self._lot_size(scanner, sym)
            probe = F.fill_live(q, Action.BUY, lot_size or 1,
                                self._fee, self._slip)
            lots = size_lots(chal_cfg, probe.price, lot_size)
            if lots <= 0:
                continue
            qty = lots * lot_size
            fill = F.fill_live(q, Action.BUY, qty, self._fee, self._slip)
            book[sym] = SPosition(
                symbol=sym, bias=sc.get("bias"), side=side, strike=q.strike,
                lots=lots, qty_units=qty, entry_price=fill.price,
                entry_fees=fill.fees, entry_ts=now.isoformat(),
                entry_score=sc.get("score") or 0.0, high_water=fill.price,
                mtm=fill.price, low_water=fill.price)
            held.add(sym)
        st["book"] = {s: p.to_json() for s, p in book.items()}
        st["closed"] = closed
        registry.set_setting(CHAL_SETTING, json.dumps(st))

    def adaptation_status(self) -> dict:
        """Backs GET /scanner/adaptation: the running trial, any pending
        proposal, embargo, tune history and last-apply measurement."""
        from app.core import registry

        def _load(key):
            try:
                raw = registry.setting(key, "")
                return json.loads(raw) if raw else None
            except (TypeError, ValueError):
                return None

        chal = _load(CHAL_SETTING)
        if chal:
            closed = chal.get("closed") or []
            chal = {"rule": chal.get("rule"), "overrides": chal.get("overrides"),
                    "started": chal.get("started"),
                    "open": len(chal.get("book") or {}), "closed_n": len(closed),
                    "expectancy": round(sum(t.get("realized") or 0
                                            for t in closed) / len(closed), 2)
                    if closed else None}
        return {"challenger": chal, "proposal": _load(PROPOSAL_SETTING),
                "embargo_until": registry.setting(EMBARGO_SETTING, "") or None,
                "history": self._tune_history()[-10:]}

    def apply_proposal(self) -> dict:
        """The human clicked Apply: take the one bounded step the trial
        validated, start the embargo, and stamp history so the change is
        measured against its pre-apply baseline."""
        from app.core import registry
        raw = registry.setting(PROPOSAL_SETTING, "")
        if not raw:
            return {"ok": False, "error": "no pending proposal"}
        p = json.loads(raw)
        cfg = self._cfg()
        now = datetime.now(IST).replace(tzinfo=None)
        frm = {k: getattr(cfg, k, None) for k in (p.get("overrides") or {})}
        for param, val in (p.get("overrides") or {}).items():
            registry.set_setting(f"scanner_trade_{param}", str(val))
        hist = self._tune_history()
        hist.append({"kind": "apply", "rule": p.get("rule"),
                     "ts": now.isoformat(), "from": frm,
                     "to": p.get("overrides"),
                     "comparison": p.get("comparison")})
        self._save_tune_history(hist)
        registry.set_setting(
            EMBARGO_SETTING,
            (now.date() + timedelta(days=A.EMBARGO_DAYS)).isoformat())
        registry.set_setting(PROPOSAL_SETTING, "")
        registry.record_event(
            "info", "insight",
            f"adaptive update APPLIED: {p.get('overrides')} (was {frm}); "
            f"new trials embargoed {A.EMBARGO_DAYS} days while it is "
            "measured against the pre-change baseline")
        return {"ok": True, "applied": p.get("overrides"), "was": frm}

    def dismiss_proposal(self) -> dict:
        from app.core import registry
        raw = registry.setting(PROPOSAL_SETTING, "")
        if not raw:
            return {"ok": False, "error": "no pending proposal"}
        p = json.loads(raw)
        hist = self._tune_history()
        hist.append({"kind": "dismiss", "rule": p.get("rule"),
                     "ts": datetime.now(IST).replace(tzinfo=None).isoformat()})
        self._save_tune_history(hist)
        registry.set_setting(PROPOSAL_SETTING, "")
        registry.record_event("info", "insight",
                              f"adaptive update dismissed ({p.get('rule')})")
        return {"ok": True}

    def _book_trade(self, sym, pos: SPosition, kind, price, fees, reason, ts,
                    realized=None):
        from app.core import registry
        row = {
            "ts": ts.isoformat(sep=" ", timespec="seconds"),
            "contract": f"{sym} {pos.strike:g} {pos.side}",
            "side": "BUY" if kind == "entry" else "SELL",
            "qty": pos.qty_units, "price": price, "fees": fees,
            "margin": 0.0, "reason": reason, "tag": f"scanner:{pos.bias}"}
        if kind == "exit" and realized is not None:
            row["net_pnl"] = round(realized, 2)
            row["gross_pnl"] = round(realized + pos.entry_fees + fees, 2)
        registry.record_trade(STRATEGY_ID, "PAPER", row)

    # -- API surface ---------------------------------------------------------
    def snapshot(self) -> dict:
        from app.core import registry
        cfg = self._cfg()
        positions = []
        unrealized = 0.0
        for p in self.book.values():
            pnl = (p.mtm - p.entry_price) * p.qty_units
            unrealized += pnl
            positions.append({
                "symbol": p.symbol, "bias": p.bias, "side": p.side,
                "strike": p.strike, "lots": p.lots, "entry": p.entry_price,
                "mtm": p.mtm, "high_water": p.high_water,
                "stop": round(effective_stop(p.entry_price, p.high_water, cfg), 2),
                "entry_ts": p.entry_ts, "entry_score": p.entry_score,
                "unrealized": round(pnl, 2)})
        positions.sort(key=lambda x: x["unrealized"], reverse=True)
        realized = registry.cum_pnl(STRATEGY_ID)
        return {
            "enabled": registry.setting("scanner_trade", "off") == "on",
            "capital": cfg.capital, "open": len(positions),
            "max_positions": cfg.max_positions,
            "realized": round(realized, 2), "unrealized": round(unrealized, 2),
            "equity": round(cfg.capital + realized + unrealized, 2),
            "positions": positions,
            "config": {"entry_score": cfg.entry_score, "exit_score": cfg.exit_score,
                       "trail_pct": cfg.trail_pct, "hard_stop_pct": cfg.hard_stop_pct,
                       "target_pct": cfg.target_pct, "risk_pct": cfg.risk_pct,
                       "reentry_cooldown_min": cfg.reentry_cooldown_min,
                       "entry_cutoff_min": cfg.entry_cutoff_min,
                       "fresh_buildup_only": cfg.fresh_buildup_only,
                       "min_volume_surge": cfg.min_volume_surge,
                       "require_liquid_chain": cfg.require_liquid_chain,
                       "min_range_align": cfg.min_range_align,
                       "index_align": cfg.index_align,
                       "max_trades_per_day": cfg.max_trades_per_day,
                       "daily_loss_stop_pct": cfg.daily_loss_stop_pct,
                       "max_entry_spread_pct": cfg.max_entry_spread_pct,
                       "require_vwap_side": cfg.require_vwap_side,
                       "require_trend_align": cfg.require_trend_align,
                       "require_structure_break": cfg.require_structure_break,
                       "max_rsi_extreme": cfg.max_rsi_extreme,
                       "max_vwap_dist_pct": cfg.max_vwap_dist_pct,
                       "min_rr": cfg.min_rr},
        }
