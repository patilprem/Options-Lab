"""scripts/vol_premium_test — the offline volatility-premium check.

In-memory DuckDB with a hand-built market (no network, no service): spot pinned
near 22000, options priced with a known time value, so every expected P&L can
be computed by hand.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import duckdb
import pytest

from app.core.contract import Action
from app.data.store import SCHEMA
from app.engines.fills import FeeConfig, SlippageConfig
from scripts.vol_premium_test import (build_trades, price_structure,
                                      suspect_cycles, summarize, verdict)

FREE = FeeConfig(brokerage_per_order=0, stt_sell_pct=0, exchange_txn_pct=0,
                 gst_pct=0, sebi_per_crore=0, stamp_buy_pct=0)
NOSLIP = SlippageConfig(bt_half_spread_base_pct=0, bt_half_spread_per_offset_pct=0)


def _store(con):
    return SimpleNamespace(_q=lambda s, p=None: con.execute(s, p or []).fetchall(),
                           _q1=lambda s, p=None: con.execute(s, p or []).fetchone())


def _market(time_value: float, spot_path=lambda i: 22000.0, weeks: int = 25):
    """Weekly Thursday expiries; each option = intrinsic + time_value*(days left)."""
    con = duckdb.connect()
    con.execute(SCHEMA)
    con.execute("CREATE TABLE expiry_calendar (underlying VARCHAR, expiry DATE)")
    d0 = date(2025, 1, 6)                                   # a Monday
    days = [d0 + timedelta(i) for i in range(weeks * 7) if (d0 + timedelta(i)).weekday() < 5]
    exps = [d for d in days if d.weekday() == 3]
    days = [d for d in days if d <= exps[-1]]
    con.executemany("INSERT INTO expiry_calendar VALUES ('NIFTY', ?)", [[e] for e in exps])
    for i, d in enumerate(days):
        s = spot_path(i)
        for t in (time(9, 15), time(10, 0), time(15, 30)):
            con.execute("INSERT INTO underlying_bars VALUES ('NIFTY',?,?,?,?,?,0,0)",
                        [datetime.combine(d, t), s, s, s, s])
        e = next(x for x in exps if x >= d)
        left = (e - d).days
        for off in range(-5, 6):
            k = round(s / 50) * 50 + 50 * off              # ATM-relative, as stored
            for ot in ("CALL", "PUT"):
                intr = max(s - k, 0) if ot == "CALL" else max(k - s, 0)
                # time value fades away from ATM, so a condor collects a credit
                px = intr + (time_value * left + 1.0) * (1 - 0.15 * abs(off))
                con.execute(
                    "INSERT INTO option_bars VALUES ('NIFTY',?,'WEEKLY',0,?,?,?,NULL,"
                    "?,?,?,?,0,0,15.0)",
                    [datetime.combine(d, time(10, 0)), off, ot, k, px, px, px, px])
    return _store(con)


def test_straddle_math_by_hand():
    """Sell 100+100 at K=22000, settle at 22150: call owes 150, put expires."""
    legs = [(0, "CALL", Action.SELL, 100.0, 22000.0), (0, "PUT", Action.SELL, 100.0, 22000.0)]
    gross, net, credit = price_structure(legs, 22150.0, 75, FREE, NOSLIP)
    assert credit == 200.0
    assert gross == pytest.approx((200 - 150) * 75)
    assert net == pytest.approx(gross)                      # zero-cost config


def test_costs_only_charged_on_legs_that_need_closing():
    legs = [(0, "CALL", Action.SELL, 100.0, 22000.0), (0, "PUT", Action.SELL, 100.0, 22000.0)]
    g_pin, n_pin, _ = price_structure(legs, 22000.0, 75, FeeConfig(), SlippageConfig())
    g_mv, n_mv, _ = price_structure(legs, 22150.0, 75, FeeConfig(), SlippageConfig())
    # at the pin both legs expire worthless: only the two entry orders cost;
    # after the move the ITM call must be bought back, which costs more
    assert 0 < g_pin - n_pin < g_mv - n_mv


def test_suspect_cycle_flags_a_missed_expiry_not_a_cadence_change():
    weekly = [date(2025, 1, 2) + timedelta(7 * i) for i in range(8)]
    missed = weekly[:4] + weekly[5:]                        # one week vanished
    assert suspect_cycles(missed) == {weekly[5]}
    monthly = [weekly[-1] + timedelta(28 * (i + 1)) for i in range(6)]
    # at most the one cycle straddling the cadence change is dropped
    assert len(suspect_cycles(weekly + monthly)) <= 1


def test_overpriced_options_show_premium_and_condor_trades():
    st = _market(time_value=8.0, spot_path=lambda i: 22000.0 + 10 * (i % 4 - 1))
    tr = build_trades(st, "NIFTY", 3, time(10, 0), 2, 2, fee=FREE, slip=NOSLIP)
    straddles = [t for t in tr if t.structure == "STRADDLE"]
    condors = [t for t in tr if t.structure == "CONDOR"]
    assert len(straddles) == len(condors) >= 20
    # Monday entry, Thursday expiry: collect 2 x (3 days x 8 + 1), pay <= 40 pts
    assert all(t.gross >= (2 * (3 * 8 + 1) - 40) * t.lot for t in straddles)
    assert all(t.max_loss and t.max_loss > 0 for t in condors)
    label, _ = verdict(straddles, condors)
    assert label.startswith(("PREMIUM PRESENT", "TRADEABLE"))


def test_underpriced_options_are_no_edge():
    """Spot swings 300 pts a day, options price almost nothing: sellers lose."""
    st = _market(time_value=0.5, spot_path=lambda i: 22000.0 + (300 if i % 2 else -300))
    tr = build_trades(st, "NIFTY", 3, time(10, 0), 2, 2)
    straddles = [t for t in tr if t.structure == "STRADDLE"]
    assert straddles and summarize(straddles, "gross")["mean"] < 0
    label, _ = verdict(straddles, [t for t in tr if t.structure == "CONDOR"])
    assert label == "NO EDGE"
