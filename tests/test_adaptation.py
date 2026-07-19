"""Tests for the champion-challenger adaptation pipeline.

Pure gates (bounded steps, persistence, comparison margins, post-apply
measurement) plus the ScannerTrader wiring: a shadow challenger trades a
virtual book on the same quotes WITHOUT touching the ledger, a matured trial
becomes a human-facing proposal, apply is bounded + embargoed, and losing
trials are discarded with a rule cooldown.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from app.core.contract import OptionQuote, OptionType
from app.engines import adaptation as A
from app.engines import scanner_trader as st


# --- pure: bounded steps ----------------------------------------------------

def test_challenger_overrides_steps_and_clamps():
    cfg = {"trail_pct": 0.25, "hard_stop_pct": 0.30, "entry_score": 65.0}
    assert A.challenger_overrides(cfg, "trail_giveback") == {"trail_pct": 0.20}
    assert A.challenger_overrides(cfg, "raise_entry_score") == {"entry_score": 70.0}
    # already at the clamp -> no step left -> None
    assert A.challenger_overrides({"trail_pct": 0.10}, "trail_giveback") is None
    # clamped, not overshot
    assert A.challenger_overrides({"trail_pct": 0.12}, "trail_giveback") == {"trail_pct": 0.10}
    # behavioural / unknown rules never self-tune
    assert A.challenger_overrides(cfg, "churn") is None
    assert A.challenger_overrides(cfg, "no_such_rule") is None


# --- pure: persistence gate -------------------------------------------------

def test_persistent_rules_needs_distinct_days():
    rows = ([{"day": "2026-07-10", "rule": "raise_entry_score"},
             {"day": "2026-07-11", "rule": "raise_entry_score"},
             {"day": "2026-07-14", "rule": "raise_entry_score"}]
            + [{"day": "2026-07-14", "rule": "trail_giveback"},
               {"day": "2026-07-14", "rule": "trail_giveback"}])  # same day 2x
    out = A.persistent_rules(rows)
    assert out == ["raise_entry_score"]        # trail fired 1 distinct day only


# --- pure: comparison margins -----------------------------------------------

def _trades(n, pnl):
    return [{"realized": pnl} for _ in range(n)]


def test_compare_books_gates_and_margin():
    # not enough challenger trades -> not ready
    assert A.compare_books(_trades(6, 100), _trades(5, 900))["ready"] is False
    # clear win
    cmp = A.compare_books(_trades(6, 100), _trades(8, 500))
    assert cmp["ready"] and cmp["better"] is True
    # a sliver (within 10% of champion's magnitude) is NOT evidence
    cmp2 = A.compare_books(_trades(6, 100), _trades(8, 105))
    assert cmp2["better"] is False
    # champion bleeding, challenger bleeding less -> still an improvement
    cmp3 = A.compare_books(_trades(6, -500), _trades(8, -100))
    assert cmp3["better"] is True


# --- pure: post-apply measurement -------------------------------------------

def test_measure_applied_verdicts():
    pre = [{"realized": 400, "ts": "2026-07-01T10:00:00"}] * 6
    post_bad = [{"realized": -300, "ts": "2026-07-20T10:00:00"}] * 8
    post_ok = [{"realized": 500, "ts": "2026-07-20T10:00:00"}] * 8
    m = A.measure_applied(pre + post_bad, "2026-07-15")
    assert m["ready"] and m["verdict"] == "worse"
    m2 = A.measure_applied(pre + post_ok, "2026-07-15")
    assert m2["verdict"] == "ok"
    # thin post-sample -> wait, no verdict
    m3 = A.measure_applied(pre + post_bad[:3], "2026-07-15")
    assert m3["ready"] is False and m3["verdict"] is None


# --- wiring: shadow book never touches the ledger ---------------------------

class _FakeHub:
    def __init__(self):
        self._chain_cache = {}

    def set_atm(self, symbol, side, ltp):
        self._chain_cache.setdefault(symbol, {})[("MONTHLY", 0, 0, side)] = OptionQuote(
            ts=datetime(2026, 7, 16, 10, 0), underlying=symbol,
            expiry=date(2026, 7, 31), strike=1250,
            option_type=OptionType.CALL if side == "CALL" else OptionType.PUT,
            ltp=ltp, bid=ltp * 0.99, ask=ltp * 1.01, oi=100000)


class _FakeScanner:
    def __init__(self):
        self.scores = {}
        self.metrics = {}
        self.tier2 = {}
        self._universe = {"RELIANCE": {"lot_size": 500, "spot_security_id": 2885}}
        self.trader = None

    def ranked_scores(self):
        return list(self.scores.values())


def _mk_trader(tmp_path, monkeypatch):
    import app.core.registry as reg
    monkeypatch.setattr(reg, "DB_PATH", str(tmp_path / "app.db"))
    reg.init_db()
    reg.set_setting("scanner_trade", "on")
    reg.set_setting("scanner_trade_risk_pct", "0.02")
    trader = st.ScannerTrader.__new__(st.ScannerTrader)
    trader.store = None
    import app.engines.fills as F
    trader._fee, trader._slip = F.FeeConfig(), F.SlippageConfig()
    trader.book = {}
    return reg, trader


def test_challenger_trades_virtually_only(tmp_path, monkeypatch):
    reg, trader = _mk_trader(tmp_path, monkeypatch)
    # champion requires score 90 (won't trade); challenger trials 65
    reg.set_setting("scanner_trade_entry_score", "90")
    reg.set_setting(st.CHAL_SETTING, json.dumps(
        {"rule": "raise_entry_score", "overrides": {"entry_score": 65.0},
         "started": "2026-07-01", "book": {}, "closed": []}))
    hub, sc = _FakeHub(), _FakeScanner()
    hub.set_atm("RELIANCE", "CALL", ltp=20.0)
    sc.scores = {"RELIANCE": {"symbol": "RELIANCE", "score": 80, "bias": "CE"}}

    trader.step(hub, sc)
    assert trader.book == {}                     # champion stayed out
    chal = json.loads(reg.setting(st.CHAL_SETTING))
    assert "RELIANCE" in chal["book"]            # challenger entered virtually
    # premium collapses -> challenger hard-stops, still virtual-only
    hub.set_atm("RELIANCE", "CALL", ltp=10.0)
    trader.step(hub, sc)
    chal = json.loads(reg.setting(st.CHAL_SETTING))
    assert chal["book"] == {} and len(chal["closed"]) == 1
    assert chal["closed"][0]["reason"] == "hard_stop"
    # the ledger never saw any of it
    assert reg.cum_pnl(st.STRATEGY_ID) == 0.0
    assert reg.journal_rows() == []


def test_persistence_starts_a_trial(tmp_path, monkeypatch):
    reg, trader = _mk_trader(tmp_path, monkeypatch)
    today = date(2026, 7, 16)
    for d in ("2026-07-12", "2026-07-14", "2026-07-15"):
        reg.record_insight_rules("scanner", d, [
            {"rule": "raise_entry_score", "suggestion": "s", "evidence": "e"}])
    trader._daily_reflection(trader._cfg(), today)
    chal = json.loads(reg.setting(st.CHAL_SETTING))
    assert chal["rule"] == "raise_entry_score"
    assert chal["overrides"] == {"entry_score": 70.0}     # one bounded step
    assert chal["started"] == today.isoformat()


def test_winning_trial_becomes_proposal_then_apply_embargoes(tmp_path, monkeypatch):
    reg, trader = _mk_trader(tmp_path, monkeypatch)
    today = date(2026, 7, 30)
    started = "2026-07-10"                       # 20 days -> past MIN_TRIAL_DAYS
    # champion: 5 modest closed trades in the window (journal rows)
    for i in range(5):
        reg.record_journal("AAA", "exit", {"realized": 100.0},
                           ts=f"2026-07-1{i+1}T11:00:00")
    # challenger: 8 clearly better virtual trades
    closed = [{"symbol": "BBB", "realized": 500.0, "reason": "trail_stop",
               "entry_ts": "2026-07-11T10:00:00", "ts": "2026-07-12T10:00:00"}] * 8
    reg.set_setting(st.CHAL_SETTING, json.dumps(
        {"rule": "raise_entry_score", "overrides": {"entry_score": 70.0},
         "started": started, "book": {}, "closed": closed}))

    trader._advance_adaptation(trader._cfg(), today, [])
    assert reg.setting(st.CHAL_SETTING) == ""            # trial concluded
    prop = json.loads(reg.setting(st.PROPOSAL_SETTING))
    assert prop["overrides"] == {"entry_score": 70.0}
    assert prop["comparison"]["better"] is True
    assert prop["current"]["entry_score"] == 65.0

    # human clicks Apply -> setting takes the bounded step, embargo starts
    res = trader.apply_proposal()
    assert res["ok"] and trader._cfg().entry_score == 70.0
    assert reg.setting(st.PROPOSAL_SETTING) == ""
    embargo = reg.setting(st.EMBARGO_SETTING)
    assert embargo == (today + timedelta(days=A.EMBARGO_DAYS)).isoformat() \
        or embargo > today.isoformat()                    # applied "now" (IST)
    hist = json.loads(reg.setting(st.TUNE_HISTORY_SETTING))
    assert hist[-1]["kind"] == "apply" and hist[-1]["from"] == {"entry_score": 65.0}

    # during the embargo, even a persistent rule starts no new trial
    for d in ("2026-07-27", "2026-07-28", "2026-07-29"):
        reg.record_insight_rules("scanner", d, [
            {"rule": "trail_giveback", "suggestion": "s", "evidence": "e"}])
    trader._advance_adaptation(trader._cfg(), today, [])
    assert reg.setting(st.CHAL_SETTING) == ""


def test_losing_trial_discarded_with_cooldown(tmp_path, monkeypatch):
    reg, trader = _mk_trader(tmp_path, monkeypatch)
    today = date(2026, 7, 30)
    for i in range(5):
        reg.record_journal("AAA", "exit", {"realized": 400.0},
                           ts=f"2026-07-1{i+1}T11:00:00")
    closed = [{"symbol": "BBB", "realized": -200.0, "reason": "hard_stop",
               "entry_ts": "2026-07-11T10:00:00", "ts": "2026-07-12T10:00:00"}] * 8
    reg.set_setting(st.CHAL_SETTING, json.dumps(
        {"rule": "raise_entry_score", "overrides": {"entry_score": 70.0},
         "started": "2026-07-10", "book": {}, "closed": closed}))

    trader._advance_adaptation(trader._cfg(), today, [])
    assert reg.setting(st.CHAL_SETTING) == ""
    assert reg.setting(st.PROPOSAL_SETTING, "") == ""     # no proposal
    hist = json.loads(reg.setting(st.TUNE_HISTORY_SETTING))
    assert hist[-1]["kind"] == "discard"
    # the discarded rule is on cooldown: persistence alone can't restart it
    for d in ("2026-07-27", "2026-07-28", "2026-07-29"):
        reg.record_insight_rules("scanner", d, [
            {"rule": "raise_entry_score", "suggestion": "s", "evidence": "e"}])
    trader._advance_adaptation(trader._cfg(), today, [])
    assert reg.setting(st.CHAL_SETTING) == ""


def test_settings_only_change_via_apply(tmp_path, monkeypatch):
    """The core invariant: reflection + trial + comparison, run end to end,
    never mutate a trading setting — only apply_proposal() does."""
    reg, trader = _mk_trader(tmp_path, monkeypatch)
    before = trader._cfg()
    for d in ("2026-07-12", "2026-07-14", "2026-07-15"):
        reg.record_insight_rules("scanner", d, [
            {"rule": "trail_giveback", "suggestion": "s", "evidence": "e"}])
    trader._daily_reflection(trader._cfg(), date(2026, 7, 16))   # starts trial
    after = trader._cfg()
    assert after == before                       # trading config untouched
