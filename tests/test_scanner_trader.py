"""Tests for the scanner-driven positional paper trader.

Pure decision logic (sizing, trailing stop, exit rules, entry pick) plus a
full step() round-trip against a fake chain cache and an isolated SQLite
registry, verifying entry -> hold -> trailing-exit and ledger booking.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.core.contract import OptionQuote, OptionType
from app.engines import scanner_trader as st
from app.engines.scanner_trader import SPosition, TradeConfig


def _pos(entry=100.0, high=100.0, bias="CE", days_ts="2026-07-16T10:00:00"):
    return SPosition(symbol="RELIANCE", bias=bias, side="CALL", strike=1250,
                     lots=1, qty_units=500, entry_price=entry, entry_fees=20.0,
                     entry_ts=days_ts, entry_score=72.0, high_water=high, mtm=entry)


# --- pure: sizing -----------------------------------------------------------

def test_size_lots_risk_budget():
    cfg = TradeConfig(capital=500_000, risk_pct=0.01, hard_stop_pct=0.30, max_lots=10)
    # risk budget = 5000; per-lot risk = 100 * 0.30 * 500 = 15000 -> 0 lots
    assert st.size_lots(cfg, 100.0, 500) == 0
    # cheaper option: premium 20 -> per-lot risk = 20*0.3*500 = 3000 -> 1 lot
    assert st.size_lots(cfg, 20.0, 500) == 1
    # tiny premium hits the max-lots cap
    assert st.size_lots(cfg, 1.0, 500) == 10
    assert st.size_lots(cfg, 0.0, 500) == 0
    assert st.size_lots(cfg, 20.0, 0) == 0


# --- pure: trailing stop ----------------------------------------------------

def test_effective_stop_ratchets_up():
    cfg = TradeConfig(hard_stop_pct=0.30, trail_pct=0.25)
    # not yet in profit: hard stop only (70% of entry), trail dormant
    assert st.effective_stop(100, 100, cfg) == 70.0
    # premium ran to 200 -> trail (150) beats the hard floor (70)
    assert st.effective_stop(100, 200, cfg) == 150.0
    # once in profit the trail engages (25% below the new high)
    assert st.effective_stop(100, 101, cfg) == round(101 * 0.75, 2)


# --- pure: exit decision ----------------------------------------------------

def test_exit_hard_stop():
    cfg = TradeConfig(hard_stop_pct=0.30, trail_pct=0.25)
    ex, why = st.exit_decision(_pos(100, 100), 65.0, None, cfg, 0)
    assert ex and why == "hard_stop"


def test_exit_trailing_stop():
    cfg = TradeConfig(hard_stop_pct=0.30, trail_pct=0.25)
    p = _pos(100, 200)                         # high-water 200 -> stop 150
    ex, why = st.exit_decision(p, 149.0, {"score": 80, "bias": "CE"}, cfg, 1)
    assert ex and why == "trail_stop"


def test_exit_on_setup_decay_and_bias_flip():
    cfg = TradeConfig(hard_stop_pct=0.30, trail_pct=0.25, exit_score=45)
    p = _pos(100, 110)
    # score decayed below exit_score
    ex, why = st.exit_decision(p, 108.0, {"score": 40, "bias": "CE"}, cfg, 1)
    assert ex and why == "setup_gone"
    # bias flipped to the other side
    ex2, why2 = st.exit_decision(p, 108.0, {"score": 80, "bias": "PE"}, cfg, 1)
    assert ex2 and why2 == "setup_gone"


def test_exit_max_hold_and_hold_otherwise():
    cfg = TradeConfig(hard_stop_pct=0.30, trail_pct=0.25, max_hold_days=10,
                      target_pct=1.0, exit_score=45)
    p = _pos(100, 110)
    ex, why = st.exit_decision(p, 108.0, {"score": 80, "bias": "CE"}, cfg, 10)
    assert ex and why == "max_hold"
    # otherwise hold
    hold, _ = st.exit_decision(p, 108.0, {"score": 80, "bias": "CE"}, cfg, 2)
    assert hold is False


# --- pure: behavioural gates (the holistic-adaptation knobs) ----------------

def _score(sym="AAA", score=80, bias="CE", buildup="long_buildup"):
    return {"symbol": sym, "score": score, "bias": bias, "buildup": buildup}


def test_defaults_leave_behaviour_unchanged():
    """All three gates default to off/no-op — pre-existing callers that pass
    no now/last_exits must see identical picks (adaptation PROPOSES change;
    shipping the knobs must not BE a change).

    Deliberately probes times BEYOND the 935 default, incl. MCX hours: the
    first version of this test used 15:34 and passed by one minute, hiding a
    real bug where the default cutoff blocked every entry after 15:35 — which
    would have silently killed ~8h of MCX trading (MCX runs to 23:30)."""
    cfg = TradeConfig()
    for hh, mm in ((15, 34), (15, 36), (18, 0), (23, 20)):
        picks = st.pick_entries(
            [_score(buildup="short_covering")], set(), cfg,
            now=datetime(2026, 7, 27, hh, mm),
            last_exits={"AAA": datetime(2026, 7, 27, 9, 30)})
        assert picks == ["AAA"], f"default gates blocked an entry at {hh}:{mm:02d}"


def test_reentry_cooldown_blocks_then_releases():
    cfg = TradeConfig(reentry_cooldown_min=30)
    exit_ts = datetime(2026, 7, 27, 11, 0)
    ranked = [_score()]
    # 10 min after the exit: blocked
    assert st.pick_entries(ranked, set(), cfg,
                           now=datetime(2026, 7, 27, 11, 10),
                           last_exits={"AAA": exit_ts}) == []
    # 31 min after: allowed again
    assert st.pick_entries(ranked, set(), cfg,
                           now=datetime(2026, 7, 27, 11, 31),
                           last_exits={"AAA": exit_ts}) == ["AAA"]
    # ISO-string exit times (the challenger's persisted form) work too
    assert st.pick_entries(ranked, set(), cfg,
                           now=datetime(2026, 7, 27, 11, 10),
                           last_exits={"AAA": exit_ts.isoformat()}) == []


def test_entry_cutoff_blocks_new_entries_only_after_time():
    cfg = TradeConfig(entry_cutoff_min=13 * 60 + 30)   # 13:30
    ranked = [_score()]
    assert st.pick_entries(ranked, set(), cfg,
                           now=datetime(2026, 7, 27, 13, 29)) == ["AAA"]
    assert st.pick_entries(ranked, set(), cfg,
                           now=datetime(2026, 7, 27, 13, 30)) == []


def test_fresh_buildup_only_skips_covering_and_unwinding():
    cfg = TradeConfig(fresh_buildup_only=1)
    ranked = [_score("AAA", buildup="short_covering"),
              _score("BBB", buildup="long_unwinding"),
              _score("CCC", buildup="short_buildup", bias="PE"),
              _score("DDD", buildup=None)]        # unknown stays allowed
    assert st.pick_entries(ranked, set(), cfg) == ["CCC", "DDD"]


# --- pure: entry pick -------------------------------------------------------

def test_pick_entries_respects_score_bias_and_slots():
    cfg = TradeConfig(entry_score=65, max_positions=2)
    ranked = [
        {"symbol": "AAA", "score": 80, "bias": "CE"},
        {"symbol": "BBB", "score": 60, "bias": "PE"},   # below entry_score
        {"symbol": "CCC", "score": 90, "bias": None},   # no bias
        {"symbol": "DDD", "score": 70, "bias": "PE"},
        {"symbol": "EEE", "score": 66, "bias": "CE"},
    ]
    picks = st.pick_entries(ranked, held=set(), cfg=cfg)
    assert picks == ["AAA", "DDD"]              # top two eligible, slot-capped
    # one slot left because AAA already held
    picks2 = st.pick_entries(ranked, held={"AAA"}, cfg=cfg)
    assert picks2 == ["DDD"]


# --- integration: entry -> exit round-trip ---------------------------------

class _FakeHub:
    def __init__(self):
        self._chain_cache = {}

    def set_atm(self, symbol, side, ltp, bid=None, ask=None):
        self._chain_cache.setdefault(symbol, {})[("MONTHLY", 0, 0, side)] = OptionQuote(
            ts=datetime(2026, 7, 16, 10, 0), underlying=symbol,
            expiry=date(2026, 7, 31), strike=1250,
            option_type=OptionType.CALL if side == "CALL" else OptionType.PUT,
            ltp=ltp, bid=bid if bid is not None else ltp * 0.99,
            ask=ask if ask is not None else ltp * 1.01, oi=100000)


class _FakeScanner:
    def __init__(self):
        self.scores = {}
        self.metrics = {"RELIANCE": {"spot": 1248.5, "ltp": 1250.0,
                                     "buildup": "long_buildup",
                                     "price_change_pct": 1.8,
                                     "volume_surge": 2.4, "range_pos": 0.9}}
        self.tier2 = {"RELIANCE": {"pcr_oi": 0.8, "atm_iv": 22.5,
                                   "iv_skew": -1.2,
                                   "liquidity": {"ok": True,
                                                 "worst_spread_pct": 0.9}}}
        self._universe = {"RELIANCE": {"lot_size": 500, "spot_security_id": 2885}}
        self.trader = None

    def ranked_scores(self):
        return list(self.scores.values())


def _iso_registry(tmp_path, monkeypatch):
    import app.core.registry as reg
    monkeypatch.setattr(reg, "DB_PATH", str(tmp_path / "app.db"))
    reg.init_db()
    return reg


def test_step_enters_then_trailing_exits(tmp_path, monkeypatch):
    reg = _iso_registry(tmp_path, monkeypatch)
    reg.set_setting("scanner_trade", "on")
    reg.set_setting("scanner_trade_entry_score", "65")
    reg.set_setting("scanner_trade_risk_pct", "0.02")

    trader = st.ScannerTrader.__new__(st.ScannerTrader)
    trader.store = None
    import app.engines.fills as F
    trader._fee, trader._slip = F.FeeConfig(), F.SlippageConfig()
    trader.book = {}

    hub, sc = _FakeHub(), _FakeScanner()
    hub.set_atm("RELIANCE", "CALL", ltp=20.0)
    sc.scores = {"RELIANCE": {"symbol": "RELIANCE", "score": 80, "bias": "CE"}}

    trader.step(hub, sc)
    assert "RELIANCE" in trader.book                # opened a CE position
    entry = trader.book["RELIANCE"].entry_price

    # premium spikes -> high-water rises, still held
    hub.set_atm("RELIANCE", "CALL", ltp=40.0)
    trader.step(hub, sc)
    assert trader.book["RELIANCE"].high_water >= 40.0

    # premium collapses below the trail (25% under 40 = 30) -> exit
    hub.set_atm("RELIANCE", "CALL", ltp=25.0)
    trader.step(hub, sc)
    assert "RELIANCE" not in trader.book
    # a realized daily row got booked
    assert reg.cum_pnl(st.STRATEGY_ID) != 0.0
    # blotter has an entry and an exit (booked under today's IST date, the
    # same clock step() uses — not a hardcoded day, which goes stale)
    today = datetime.now(st.IST).date().isoformat()
    trades = reg.trades_for(st.STRATEGY_ID, today, "PAPER")
    assert len([t for t in trades if t.get("reason") == "entry"]) == 1
    assert len([t for t in trades if t.get("side") == "SELL"]) == 1

    # the rich journal captured the full round trip
    entries = reg.journal_rows(kind="entry")
    exits = reg.journal_rows(kind="exit")
    assert len(entries) == 1 and len(exits) == 1
    e, x = entries[0], exits[0]
    # entry: setup context frozen at fill time
    assert e["symbol"] == "RELIANCE" and e["entry_price"] == entry
    assert e["entry_ctx"]["buildup"] == "long_buildup"
    assert e["entry_ctx"]["spot"] == 1248.5
    assert e["entry_ctx"]["atm_iv"] == 22.5
    assert e["entry_ctx"]["config"]["trail_pct"] == 0.25
    # exit: self-contained round trip with excursions and durations
    assert x["reason"] == "trail_stop" and x["entry_price"] == entry
    assert x["realized"] != 0 and x["ret_pct"] is not None
    assert x["mfe_pct"] > 90                       # premium 20 -> 40 peak
    assert x["mae_pct"] >= 0 and x["mae_pct"] < 5  # never traded below entry
    assert x["held_minutes"] is not None and x["held_days"] == 0
    assert x["exit_score"] == 80                   # score snapshot at exit
    assert x["entry_ctx"]["volume_surge"] == 2.4   # entry ctx rides along
    # the daily reflection hook ran (and, with 1 trade, suggested nothing)
    assert reg.setting("scanner_insight_day") == today


def test_daily_pnl_accumulates_across_cycles_same_day(tmp_path, monkeypatch):
    """daily_pnl is one row per (strategy, mode, date) that step() rewrites
    every cycle — two round trips closed in two SEPARATE cycles the same day
    must SUM in that row. A naive per-cycle overwrite would leave only the
    second exit's realized, silently erasing the first."""
    reg = _iso_registry(tmp_path, monkeypatch)
    reg.set_setting("scanner_trade", "on")
    reg.set_setting("scanner_trade_entry_score", "65")
    reg.set_setting("scanner_trade_risk_pct", "0.02")

    trader = st.ScannerTrader.__new__(st.ScannerTrader)
    trader.store = None
    import app.engines.fills as F
    trader._fee, trader._slip = F.FeeConfig(), F.SlippageConfig()
    trader.book = {}

    hub, scanner = _FakeHub(), _FakeScanner()
    scanner.scores = {"RELIANCE": {"symbol": "RELIANCE", "score": 80, "bias": "CE"}}

    # round trip 1: enter, run up, trailing-exit in profit
    hub.set_atm("RELIANCE", "CALL", ltp=20.0)
    trader.step(hub, scanner)
    hub.set_atm("RELIANCE", "CALL", ltp=40.0)
    trader.step(hub, scanner)
    hub.set_atm("RELIANCE", "CALL", ltp=25.0)          # below trail (30) -> exit
    trader.step(hub, scanner)
    assert "RELIANCE" not in trader.book

    # round trip 2: enter again, hard-stop out — same day, separate cycles
    hub.set_atm("RELIANCE", "CALL", ltp=20.0)
    trader.step(hub, scanner)
    hub.set_atm("RELIANCE", "CALL", ltp=13.0)          # 35% down -> hard stop
    trader.step(hub, scanner)
    assert "RELIANCE" not in trader.book

    exits = reg.journal_rows(kind="exit")
    assert len(exits) == 2
    expected_total = sum(e["realized"] for e in exits)

    today = datetime.now(st.IST).date().isoformat()
    row = next(r for r in reg.performance_rows(st.STRATEGY_ID, "PAPER")
              if r["trade_date"] == today)
    assert row["realized"] == pytest.approx(expected_total, abs=0.01)


def test_manage_marks_and_exits_without_opening_new_positions(tmp_path, monkeypatch):
    """manage() is the fast, frequently-callable half of step() — it must mark
    and exit existing positions exactly like step() does, but NEVER open a
    new one even when a qualifying candidate is sitting right there. Only the
    full step() (gated behind the slower Tier-2 cycle) does discovery."""
    reg = _iso_registry(tmp_path, monkeypatch)
    reg.set_setting("scanner_trade", "on")
    reg.set_setting("scanner_trade_entry_score", "65")
    reg.set_setting("scanner_trade_risk_pct", "0.02")

    trader = st.ScannerTrader.__new__(st.ScannerTrader)
    trader.store = None
    import app.engines.fills as F
    trader._fee, trader._slip = F.FeeConfig(), F.SlippageConfig()
    trader.book = {}

    hub, scanner = _FakeHub(), _FakeScanner()
    hub.set_atm("RELIANCE", "CALL", ltp=20.0)
    scanner.scores = {"RELIANCE": {"symbol": "RELIANCE", "score": 80, "bias": "CE"}}

    exited = trader.manage(hub, scanner)
    assert exited == set()
    assert "RELIANCE" not in trader.book       # no entry from manage() alone

    # now the same candidate opens fine through the real step()
    trader.step(hub, scanner)
    assert "RELIANCE" in trader.book

    # mark it up, then have manage() alone catch the trailing-stop exit
    hub.set_atm("RELIANCE", "CALL", ltp=40.0)
    trader.manage(hub, scanner)
    hub.set_atm("RELIANCE", "CALL", ltp=25.0)   # below trail (30) -> exit
    exited2 = trader.manage(hub, scanner)
    assert exited2 == {"RELIANCE"}
    assert "RELIANCE" not in trader.book
    today = datetime.now(st.IST).date().isoformat()
    row = next(r for r in reg.performance_rows(st.STRATEGY_ID, "PAPER")
              if r["trade_date"] == today)
    assert row["realized"] > 0                  # the trailing exit was booked


def test_cooldown_blocks_immediate_rebuy_after_exit(tmp_path, monkeypatch):
    """End-to-end churn guard: with reentry_cooldown_min set, a symbol that
    just exited is NOT re-bought on the next cycle even though its score
    still clears entry_score (the exact INFY 07-24 failure: 9 re-entries on
    a persistently-elevated score, net -Rs15,327)."""
    reg = _iso_registry(tmp_path, monkeypatch)
    reg.set_setting("scanner_trade", "on")
    reg.set_setting("scanner_trade_risk_pct", "0.02")
    reg.set_setting("scanner_trade_reentry_cooldown_min", "30")

    trader = st.ScannerTrader.__new__(st.ScannerTrader)
    trader.store = None
    import app.engines.fills as F
    trader._fee, trader._slip = F.FeeConfig(), F.SlippageConfig()
    trader.book = {}

    hub, scanner = _FakeHub(), _FakeScanner()
    scanner.scores = {"RELIANCE": {"symbol": "RELIANCE", "score": 80, "bias": "CE"}}

    hub.set_atm("RELIANCE", "CALL", ltp=20.0)
    trader.step(hub, scanner)
    assert "RELIANCE" in trader.book
    hub.set_atm("RELIANCE", "CALL", ltp=13.0)      # 35% down -> hard stop
    trader.step(hub, scanner)
    assert "RELIANCE" not in trader.book

    # score is still 80 and a fresh cycle runs — cooldown must block the rebuy
    hub.set_atm("RELIANCE", "CALL", ltp=13.5)
    trader.step(hub, scanner)
    assert "RELIANCE" not in trader.book


def test_entry_ctx_thin_window_reports_null_not_zero(tmp_path, monkeypatch):
    """A thin premium window must read as NO DATA, not as a real measurement.

    The original behaviour recorded 0.00 on a first-ever entry (VWAP of one
    sample == that price), which is indistinguishable from "entered exactly at
    VWAP". In the 07-24 data every 0.00 row was a symbol's first entry and
    every populated row was a re-entry, so the rows carrying data were ~70%
    INFY churn — bucketing them would have produced a confident wrong answer.
    Now: None until there's enough history, plus n_samples so it's filterable."""
    reg = _iso_registry(tmp_path, monkeypatch)
    reg.set_setting("scanner_trade", "on")
    reg.set_setting("scanner_trade_risk_pct", "0.02")

    trader = st.ScannerTrader.__new__(st.ScannerTrader)
    trader.store = None
    import app.engines.fills as F
    trader._fee, trader._slip = F.FeeConfig(), F.SlippageConfig()
    trader.book = {}

    hub, scanner = _FakeHub(), _FakeScanner()
    hub.set_atm("RELIANCE", "CALL", ltp=20.0)
    scanner.scores = {"RELIANCE": {"symbol": "RELIANCE", "score": 80, "bias": "CE"}}

    trader.step(hub, scanner)
    ctx = trader.book["RELIANCE"].entry_ctx
    assert ctx["opt_prem_samples"] == 1
    assert ctx["opt_dist_to_vwap_pct"] is None      # was 0.0 — the bug
    assert ctx["opt_pct_b"] is None                 # needs >= 5 samples


def test_candidates_are_sampled_even_when_not_traded(tmp_path, monkeypatch):
    """The bias fix: a ranked candidate accumulates history whether or not we
    trade it, so its FIRST entry already has a real VWAP to compare against."""
    reg = _iso_registry(tmp_path, monkeypatch)
    reg.set_setting("scanner_trade", "on")
    reg.set_setting("scanner_trade_entry_score", "95")   # nothing qualifies

    trader = st.ScannerTrader.__new__(st.ScannerTrader)
    trader.store = None
    import app.engines.fills as F
    trader._fee, trader._slip = F.FeeConfig(), F.SlippageConfig()
    trader.book = {}

    hub, scanner = _FakeHub(), _FakeScanner()
    scanner.scores = {"RELIANCE": {"symbol": "RELIANCE", "score": 80, "bias": "CE"}}

    for ltp in (20.0, 21.0, 22.0, 23.0, 24.0, 25.0):
        hub.set_atm("RELIANCE", "CALL", ltp=ltp)
        trader.step(hub, scanner)

    assert trader.book == {}                          # never entered
    assert len(trader._prem_hist[("RELIANCE", "CALL")]) == 6   # but sampled

    # now let it qualify: the very first entry has a real window behind it
    reg.set_setting("scanner_trade_entry_score", "65")
    reg.set_setting("scanner_trade_risk_pct", "0.02")
    hub.set_atm("RELIANCE", "CALL", ltp=21.0)
    trader.step(hub, scanner)
    ctx = trader.book["RELIANCE"].entry_ctx
    assert ctx["opt_prem_samples"] >= 5
    assert ctx["opt_dist_to_vwap_pct"] is not None    # real, not 0.0
    assert 0.0 <= ctx["opt_pct_b"] <= 1.0             # %B is normalised


def test_analyze_over_real_journal_rows(tmp_path, monkeypatch):
    """reflect() end-to-end: journal rows written by the trader feed straight
    into journal_insights.analyze without any adapter."""
    reg = _iso_registry(tmp_path, monkeypatch)
    reg.set_setting("scanner_trade", "on")
    reg.set_setting("scanner_trade_risk_pct", "0.02")

    trader = st.ScannerTrader.__new__(st.ScannerTrader)
    trader.store = None
    import app.engines.fills as F
    trader._fee, trader._slip = F.FeeConfig(), F.SlippageConfig()
    trader.book = {}

    hub, sc = _FakeHub(), _FakeScanner()
    hub.set_atm("RELIANCE", "CALL", ltp=20.0)
    sc.scores = {"RELIANCE": {"symbol": "RELIANCE", "score": 80, "bias": "CE"}}
    trader.step(hub, sc)                            # enter
    hub.set_atm("RELIANCE", "CALL", ltp=10.0)       # -50% -> hard stop
    trader.step(hub, sc)
    assert "RELIANCE" not in trader.book

    res = trader.reflect()
    assert res["overall"]["n"] == 1
    assert res["ready"] is False                    # honest below MIN_TRADES
    assert res["by_reason"]["hard_stop"]["n"] == 1
    assert res["config"]["hard_stop_pct"] == 0.30


def test_step_noop_when_disabled(tmp_path, monkeypatch):
    reg = _iso_registry(tmp_path, monkeypatch)
    reg.set_setting("scanner_trade", "off")
    trader = st.ScannerTrader.__new__(st.ScannerTrader)
    trader.store = None
    import app.engines.fills as F
    trader._fee, trader._slip = F.FeeConfig(), F.SlippageConfig()
    trader.book = {}
    hub, sc = _FakeHub(), _FakeScanner()
    hub.set_atm("RELIANCE", "CALL", 20.0)
    sc.scores = {"RELIANCE": {"symbol": "RELIANCE", "score": 90, "bias": "CE"}}
    trader.step(hub, sc)
    assert trader.book == {}
