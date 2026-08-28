"""Tests for the entry-gate replay backtest (scripts/entry_gate_replay.py).

Pure logic only — synthetic journal rows + a real DataStore on tmp_path (the
test_expiry_relabel pattern). The script's DB-copy I/O is not exercised here
(it needs the VPS's live files); everything decision-shaped is.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.data.store import DataStore
from app.engines.scanner_trader import TradeConfig
from scripts.entry_gate_replay import (apply_breakers, build_candidate,
                                       gate_label, gates_failed_independently,
                                       is_data_missing, judge_trades,
                                       reconstruct_tech, score_gate_overlap,
                                       summarize)

ENTRY_DAY = "2026-08-20"
OPEN = datetime(2026, 8, 20, 9, 15)


def _tech(bias="CE", **kw):
    up = bias == "CE"
    d = {"n_bars": 60, "ltp": 100.0, "vwap": 99.0 if up else 101.0,
         "vwap_dist_pct": 1.0 if up else -1.0, "trend": "up" if up else "down",
         "rsi": 60.0 if up else 40.0, "or_high": 99.5, "or_low": 98.5,
         "prev_high": 99.0, "prev_low": 96.0, "prev_close": 98.0,
         "structure_break": "up" if up else "down",
         "break_level": 99.5 if up else 100.5,
         "target_ce": 104.0, "stop_ce": 98.0, "rr_ce": 2.0,
         "target_pe": 96.0, "stop_pe": 102.0, "rr_pe": 2.0}
    d.update(kw)
    return d


def _row(sym="AAA", bias="CE", realized=-1000.0,
         entry_ts=f"{ENTRY_DAY}T10:00:00", ts=f"{ENTRY_DAY}T11:00:00",
         score=80, ctx=None):
    base_ctx = {"buildup": "long_buildup", "volume_surge": 2.0,
                "range_pos": 0.9 if bias == "CE" else 0.1,
                "worst_spread_pct": 1.0, "opt_spread_pct": 1.0,
                "market_bias": None}
    base_ctx.update(ctx or {})
    return {"symbol": sym, "bias": bias, "entry_score": score,
            "realized": realized, "entry_ts": entry_ts, "ts": ts,
            "reason": "hard_stop", "kind": "exit", "entry_ctx": base_ctx}


def _judge(rows, techs, cfg=None):
    cfg = cfg or TradeConfig()
    tech_by_key = {(r["symbol"], r["entry_ts"]): t
                   for r, t in zip(rows, techs)}
    return judge_trades(rows, tech_by_key, cfg)


# --- candidate assembly -----------------------------------------------------

def test_build_candidate_merges_top_level_bias_and_proxies_liquidity():
    row = _row(ctx={"worst_spread_pct": 1.0})
    sc = build_candidate(row, _tech())
    assert sc["bias"] == "CE" and sc["score"] == 80
    assert sc["liquidity_ok"] is True
    assert sc["tech"]["trend"] == "up"
    # wide chain -> would have failed the screen
    assert build_candidate(_row(ctx={"worst_spread_pct": 3.0}),
                           None)["liquidity_ok"] is False
    # never measured -> not vetted (None), and no tech key when None
    sc3 = build_candidate(_row(ctx={"worst_spread_pct": None}), None)
    assert sc3["liquidity_ok"] is None and "tech" not in sc3


# --- signal verdicts --------------------------------------------------------

def test_confluent_trade_is_kept_and_rsi_fail_is_blocked():
    rows = [_row("AAA"), _row("BBB", entry_ts=f"{ENTRY_DAY}T10:05:00")]
    verdicts = _judge(rows, [_tech(), _tech(rsi=85.0)])
    ok = {v["symbol"]: v for v in verdicts}
    assert ok["AAA"]["passed"] and ok["AAA"]["gate"] is None
    assert not ok["BBB"]["passed"] and ok["BBB"]["gate"] == "rsi"
    assert ok["BBB"]["data_missing"] is False       # a reading said no


def test_missing_tech_blocks_and_is_flagged_missing():
    v = _judge([_row()], [None])[0]
    assert not v["passed"] and v["gate"] == "tech_missing"
    assert v["data_missing"] is True


def test_pre_instrumentation_row_is_flagged_missing():
    # range_pos was not recorded before ~07-27 — blocked, but as missing data
    v = _judge([_row(ctx={"range_pos": None})], [_tech()])[0]
    assert not v["passed"] and v["data_missing"] is True
    assert v["gate"] == "range_align"


def test_fill_time_spread_gate_applies_after_signal_gates():
    wide = _judge([_row(ctx={"opt_spread_pct": 5.0})], [_tech()])[0]
    assert not wide["passed"] and wide["gate"] == "spread"
    assert wide["data_missing"] is False
    unknown = _judge([_row(ctx={"opt_spread_pct": None})], [_tech()])[0]
    assert not unknown["passed"] and unknown["data_missing"] is True


# --- day-level circuit breakers ---------------------------------------------

def test_trade_cap_blocks_third_entry_of_the_day():
    cfg = TradeConfig(max_trades_per_day=2, daily_loss_stop_pct=0)
    rows = [_row("AAA", entry_ts=f"{ENTRY_DAY}T09:40:00", realized=500),
            _row("BBB", entry_ts=f"{ENTRY_DAY}T10:00:00", realized=500),
            _row("CCC", entry_ts=f"{ENTRY_DAY}T10:20:00", realized=500),
            # next day starts fresh
            _row("DDD", entry_ts="2026-08-21T09:40:00",
                 ts="2026-08-21T11:00:00", realized=500)]
    verdicts = _judge(rows, [_tech()] * 4, cfg)
    surviving = apply_breakers(verdicts, cfg)
    kept = {verdicts[i]["symbol"] for i in surviving}
    assert kept == {"AAA", "BBB", "DDD"}
    ccc = next(v for v in verdicts if v["symbol"] == "CCC")
    assert ccc["breaker"] == "trade cap"


def test_daily_loss_stop_blocks_after_realized_breach():
    cfg = TradeConfig(capital=500_000, daily_loss_stop_pct=0.02,
                      max_trades_per_day=0)          # -10k halts
    rows = [_row("AAA", entry_ts=f"{ENTRY_DAY}T09:40:00",
                 ts=f"{ENTRY_DAY}T10:30:00", realized=-15_000),
            # entered AFTER AAA's exit booked the -15k -> blocked
            _row("BBB", entry_ts=f"{ENTRY_DAY}T11:00:00", realized=2_000),
            # entered BEFORE the loss was realized -> allowed
            _row("CCC", entry_ts=f"{ENTRY_DAY}T10:00:00",
                 ts=f"{ENTRY_DAY}T14:00:00", realized=1_000)]
    verdicts = _judge(rows, [_tech()] * 3, cfg)
    surviving = apply_breakers(verdicts, cfg)
    kept = {verdicts[i]["symbol"] for i in surviving}
    assert kept == {"AAA", "CCC"}
    bbb = next(v for v in verdicts if v["symbol"] == "BBB")
    assert bbb["breaker"] == "daily-loss stop"


# --- summary math -----------------------------------------------------------

def test_summary_conserves_pnl_and_attributes_gates():
    cfg = TradeConfig()
    rows = [_row("AAA", realized=2_000),                       # kept
            _row("BBB", realized=-3_000,
                 entry_ts=f"{ENTRY_DAY}T10:05:00",
                 ctx={"volume_surge": 1.0}),                   # blocked: surge
            _row("CCC", realized=-4_000,
                 entry_ts=f"{ENTRY_DAY}T10:10:00")]            # blocked: tech
    verdicts = _judge(rows, [_tech(), _tech(), None], cfg)
    s = summarize(verdicts, cfg)
    assert s["actual"]["n"] == 3 and s["actual"]["net"] == -5_000
    assert s["kept"]["net"] + s["blocked"]["net"] == pytest.approx(-5_000)
    assert s["by_gate"]["volume_surge"]["n"] == 1
    assert s["by_gate"]["volume_surge"]["net"] == -3_000
    assert s["by_gate"]["tech_missing"]["n"] == 1
    assert s["missing"]["n"] == 1                    # only CCC is data-missing
    # policy rows: signal-only forgives the missing-data block (CCC kept)
    names = {name: st for name, st in s["policies"]}
    assert names["Signal gates only (missing data forgiven)"]["n"] == 2
    assert names["Full checklist (missing data = blocked, as live)"]["n"] == 1


# --- independent gate overlap (the entry_score drill-down) -----------------

def test_gates_failed_independently_isolates_a_single_problem():
    cfg = TradeConfig()
    sc = build_candidate(_row(score=60), _tech())    # everything else clean
    assert gates_failed_independently(sc, cfg) == {"entry_score"}


def test_gates_failed_independently_finds_every_independent_failure():
    cfg = TradeConfig()
    sc = build_candidate(
        _row(score=60, ctx={"volume_surge": 1.0}), _tech(rsi=85.0))
    failed = gates_failed_independently(sc, cfg)
    assert failed == {"entry_score", "volume_surge", "rsi"}


def test_missing_tech_collapses_to_one_bucket_not_six():
    cfg = TradeConfig()
    sc = build_candidate(_row(score=60), None)       # no snapshot series
    failed = gates_failed_independently(sc, cfg)
    assert "tech_missing" in failed
    # none of the six individual technical labels appear alongside it
    assert not failed & {"vwap_side", "trend_align", "structure_break",
                         "rsi", "vwap_dist", "risk_reward"}


def test_no_bias_short_circuits():
    cfg = TradeConfig()
    sc = build_candidate(_row(score=60), _tech())
    sc["bias"] = None
    assert gates_failed_independently(sc, cfg) == {"no_bias"}


def test_score_gate_overlap_splits_score_only_from_also_other():
    cfg = TradeConfig()
    rows = [
        # blocked by score alone — everything else would pass
        _row("AAA", score=60, realized=-1000,
             entry_ts=f"{ENTRY_DAY}T09:40:00"),
        # blocked by score, and independently fails volume_surge too
        _row("BBB", score=60, realized=-2000,
             entry_ts=f"{ENTRY_DAY}T09:45:00",
             ctx={"volume_surge": 1.0}),
        # not blocked by score at all — must not appear in the overlap
        _row("CCC", score=90, realized=500,
             entry_ts=f"{ENTRY_DAY}T09:50:00"),
    ]
    verdicts = _judge(rows, [_tech(), _tech(), _tech()], cfg)
    overlap = score_gate_overlap(verdicts, cfg)
    assert overlap["n"] == 2
    assert overlap["only_score"]["n"] == 1
    assert overlap["only_score"]["net"] == -1000
    assert overlap["also_other"]["n"] == 1
    assert overlap["also_other"]["net"] == -2000
    assert overlap["other_gate_counts"] == {"volume_surge": 1}


def test_score_gate_overlap_empty_when_nothing_blocked_by_score():
    cfg = TradeConfig()
    verdicts = _judge([_row("AAA")], [_tech()], cfg)   # passes everything
    overlap = score_gate_overlap(verdicts, cfg)
    assert overlap["n"] == 0
    assert overlap["only_score"]["n"] == 0 and overlap["also_other"]["n"] == 0


# --- point-in-time tech reconstruction ---------------------------------------

def _snap(sym, ts, ltp, cum_vol, hi=None, lo=None):
    return {"symbol": sym, "ts": ts, "spot": None, "fut_ltp": ltp,
            "day_open": None, "day_high": hi if hi is not None else ltp,
            "day_low": lo if lo is not None else ltp, "prev_close": None,
            "volume": cum_vol, "oi": 1000.0}


def test_reconstruct_tech_truncates_at_entry_ts(tmp_path):
    store = DataStore(tmp_path / "r.duckdb")
    rows = []
    # prev session for pivot levels
    prev = datetime(2026, 8, 19, 9, 15)
    rows.append(_snap("AAA", prev, 108.0, 1000, hi=110, lo=100))
    # entry day: 40 quiet minutes, then a huge spike AFTER the entry
    for i in range(40):
        rows.append(_snap("AAA", OPEN + timedelta(minutes=i),
                          100.0 + i * 0.1, 1000 * (i + 1)))
    for i in range(5):
        rows.append(_snap("AAA", OPEN + timedelta(minutes=40 + i),
                          200.0, 1000 * (41 + i)))
    store.upsert_stock_snapshots(rows)

    entry_ts = (OPEN + timedelta(minutes=39)).isoformat()   # before the spike
    trades = [_row("AAA", entry_ts=entry_ts)]
    tech = reconstruct_tech(store, trades)[("AAA", entry_ts)]
    assert tech["n_bars"] == 40                      # spike bars excluded
    assert tech["ltp"] == pytest.approx(103.9)       # price AT entry, not 200
    assert tech["prev_close"] == 108.0               # prior session's last ltp
    # sanity: including the spike would move ltp — prove the truncation bit
    late_ts = (OPEN + timedelta(minutes=44)).isoformat()
    trades2 = [_row("AAA", entry_ts=late_ts)]
    tech2 = reconstruct_tech(store, trades2)[("AAA", late_ts)]
    assert tech2["ltp"] == 200.0 and tech2["n_bars"] == 45


# --- label helpers -----------------------------------------------------------

def test_gate_labels_and_missing_markers():
    assert gate_label("no technical read (no snapshot series)") == "tech_missing"
    assert gate_label("RSI 82 overextended (> 75)") == "rsi"
    assert gate_label("no confirmed break-and-hold of OR/prev-day high") \
        == "structure_break"
    assert gate_label("R:R 1.2 < 1.5") == "risk_reward"
    assert gate_label("CE against market bias -0.50") == "index_align"
    assert is_data_missing("volume surge unknown (no baseline)")
    assert not is_data_missing("volume 1.2x < 1.5x required")
    assert not is_data_missing("no confirmed break-and-hold of OR/prev-day high")
