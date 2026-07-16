"""Tests for the scanner-driven positional paper trader.

Pure decision logic (sizing, trailing stop, exit rules, entry pick) plus a
full step() round-trip against a fake chain cache and an isolated SQLite
registry, verifying entry -> hold -> trailing-exit and ledger booking.
"""

from __future__ import annotations

from datetime import date, datetime

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
    # blotter has an entry and an exit
    trades = reg.trades_for(st.STRATEGY_ID, date(2026, 7, 16).isoformat(), "PAPER")
    assert len([t for t in trades if t.get("reason") == "entry"]) == 1
    assert len([t for t in trades if t.get("side") == "SELL"]) == 1


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
