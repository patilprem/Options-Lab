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
ENTRY  a shortlisted setup scoring >= entry_score, with a CE/PE bias and a
       liquid chain (the score already caps illiquid names), that we don't
       already hold and have a free slot for → buy the ATM option of the bias
       side, sized to risk a fixed % of capital.
HOLD   marked to the live chain each cycle; the stop ratchets UP as the premium
       makes new highs (never down).
EXIT   whichever fires first: hard stop, trailing stop, target, max holding
       period, OR the setup decays (score < exit_score) / the bias flips.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone, timedelta

from app.core.contract import Action
from app.engines import adaptation as A
from app.engines import fills as F

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
    entry_score: float = 65.0        # min setup score to open
    exit_score: float = 45.0         # setup decayed below this -> exit
    hard_stop_pct: float = 0.30      # initial stop: 30% below entry premium
    trail_pct: float = 0.25          # trail 25% below the high-water premium
    target_pct: float = 1.00         # optional take-profit (+100%); 0 = off
    max_positions: int = 5
    max_hold_days: int = 10          # positional, but not forever
    max_lots: int = 10


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


def pick_entries(ranked_scores: list, held: set, cfg: TradeConfig) -> list:
    """Symbols to open this cycle: highest-scoring setups above entry_score,
    with a bias, not already held, up to the free-slot count."""
    slots = cfg.max_positions - len(held)
    if slots <= 0:
        return []
    out = []
    for sc in ranked_scores:
        sym = sc.get("symbol")
        if not sym or sym in held or not sc.get("bias"):
            continue
        if (sc.get("score") or 0) < cfg.entry_score:
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
            entry_score=_f("scanner_trade_entry_score", 65.0),
            exit_score=_f("scanner_trade_exit_score", 45.0),
            hard_stop_pct=_f("scanner_trade_hard_stop_pct", 0.30),
            trail_pct=_f("scanner_trade_trail_pct", 0.25),
            target_pct=_f("scanner_trade_target_pct", 1.00),
            max_positions=int(_f("scanner_trade_max_positions", 5)),
            max_hold_days=int(_f("scanner_trade_max_hold_days", 10)),
            max_lots=int(_f("scanner_trade_max_lots", 10)),
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

    # -- the step ------------------------------------------------------------
    def step(self, hub, scanner) -> None:
        """One management pass: mark + exit open positions, then open new ones.
        Called each Tier-2 cycle. No-op unless `scanner_trade` is on."""
        from app.core import registry
        if registry.setting("scanner_trade", "off") != "on":
            return
        cfg = self._cfg()
        now = datetime.now(IST).replace(tzinfo=None)
        day = now.date()
        realized_today = 0.0
        fees_today = 0.0

        # 1) manage / exit existing positions
        exited: set[str] = set()
        for sym, pos in list(self.book.items()):
            q = self._atm_quote(hub, sym, pos.side)
            if q is None or not q.ltp:
                continue                               # no live quote -> hold
            premium = q.ltp
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
            self._book_trade(sym, pos, "exit", fill.price, fill.fees, reason, now)
            self._journal_exit(sym, pos, fill, reason, realized, scanner, now,
                              held_days)
            registry.record_event(
                "info", "scanner",
                f"trade EXIT {sym} {pos.bias} @ {fill.price} ({reason}) "
                f"P&L ₹{round(realized)}")
            del self.book[sym]
            exited.add(sym)

        # a closed trade is new evidence — reflect on the journal (at most
        # once a day) and surface any data-backed suggestion as an event
        if exited:
            self._daily_reflection(cfg, day)

        # 2) open new positions from the freshest ranked setups. Names exited
        # THIS cycle are held out so a trailing-stop exit can't immediately
        # re-buy the same name on the still-elevated score (churn).
        held = set(self.book) | exited
        for sym in pick_entries(scanner.ranked_scores(), held, cfg):
            sc = scanner.scores.get(sym) or {}
            side = self._side_for(sc.get("bias"))
            q = self._atm_quote(hub, sym, side)
            if q is None or not (q.ask or q.ltp):
                continue
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

        # 2b) step the shadow challenger (if a config trial is running) on the
        # same scores/quotes — virtual book, never touches the ledger
        try:
            self._step_challenger(hub, scanner, now, day)
        except Exception:
            pass

        # 3) book the day's P&L + persist the open book
        unrealized = sum((p.mtm - p.entry_price) * p.qty_units
                         for p in self.book.values())
        if realized_today or fees_today or self.book:
            cum = registry.cum_pnl(STRATEGY_ID) + realized_today
            equity = cfg.capital + cum + unrealized
            registry.save_paper_day(STRATEGY_ID, day.isoformat(),
                                    realized_today, unrealized, fees_today, equity)
        self._persist()

    # -- journal (rich per-trade log for strategy improvement) ---------------
    @staticmethod
    def _entry_context(scanner, sym: str, sc: dict, q, cfg: TradeConfig) -> dict:
        """Everything known about the setup at the moment of entry — Tier-1
        read, Tier-2 chain state, the option's own quote, and the config that
        sized the trade. Journaled now and again with the exit, so every
        closed trade is a self-contained record for later analysis."""
        t1 = (getattr(scanner, "metrics", None) or {}).get(sym) or {}
        t2 = (getattr(scanner, "tier2", None) or {}).get(sym) or {}
        spread_pct = None
        if q.bid and q.ask and (q.bid + q.ask) > 0:
            spread_pct = round((q.ask - q.bid) / ((q.ask + q.bid) / 2) * 100, 2)
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
            "worst_spread_pct": (t2.get("liquidity") or {}).get("worst_spread_pct"),
            "opt_bid": q.bid, "opt_ask": q.ask, "opt_ltp": q.ltp,
            "opt_iv": getattr(q, "iv", None), "opt_oi": q.oi,
            "opt_spread_pct": spread_pct,
            "expiry": str(q.expiry) if getattr(q, "expiry", None) else None,
            "config": {"entry_score": cfg.entry_score,
                       "exit_score": cfg.exit_score,
                       "hard_stop_pct": cfg.hard_stop_pct,
                       "trail_pct": cfg.trail_pct,
                       "target_pct": cfg.target_pct,
                       "risk_pct": cfg.risk_pct},
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
                    "info", "scanner",
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
                        "warn", "scanner",
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
                        "info", "scanner",
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
                        "info", "scanner",
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
                    "info", "scanner",
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
                "info", "scanner",
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
        for sym in pick_entries(scanner.ranked_scores(), held, chal_cfg):
            sc = scanner.scores.get(sym) or {}
            side = self._side_for(sc.get("bias"))
            q = self._atm_quote(hub, sym, side)
            if q is None or not (q.ask or q.ltp):
                continue
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
            "info", "scanner",
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
        registry.record_event("info", "scanner",
                              f"adaptive update dismissed ({p.get('rule')})")
        return {"ok": True}

    def _book_trade(self, sym, pos: SPosition, kind, price, fees, reason, ts):
        from app.core import registry
        registry.record_trade(STRATEGY_ID, "PAPER", {
            "ts": ts.isoformat(sep=" ", timespec="seconds"),
            "contract": f"{sym} {pos.strike:g} {pos.side}",
            "side": "BUY" if kind == "entry" else "SELL",
            "qty": pos.qty_units, "price": price, "fees": fees,
            "margin": 0.0, "reason": reason, "tag": f"scanner:{pos.bias}"})

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
                       "target_pct": cfg.target_pct, "risk_pct": cfg.risk_pct},
        }
