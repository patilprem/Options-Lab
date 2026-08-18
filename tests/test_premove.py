"""Pre-move clue sweep — finding the tell without being told when to look.

Offline. Proves the move finder picks NET moves (not whipsaws) and collapses
overlaps, that scoring is baseline-relative and refuses to speak on small N,
that a gap in the chain timeline never gets bridged into a fake delta, and
that a planted pre-move signal is actually surfaced end to end.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.core.contract import Bar
from app.data.store import DataStore
from app.engines import premove


@pytest.fixture
def store(tmp_path):
    return DataStore(tmp_path / "md.duckdb")


def _bars(start, closes, interval=5):
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append(Bar(start + timedelta(minutes=i * interval), o,
                       max(o, c) + 1, min(o, c) - 1, c, 1000))
    return out


# --- find_moves -------------------------------------------------------------

def test_finds_the_biggest_net_move():
    t0 = datetime(2026, 8, 17, 9, 15)
    closes = [100.0] * 12 + [100 + 3 * i for i in range(1, 7)] + [118.0] * 6
    moves = premove.find_moves(_bars(t0, closes), window_min=30, interval_min=5)
    assert moves
    assert moves[0]["move"] > 15
    assert moves[0]["start"].hour == 10


def test_whipsaw_scores_below_a_real_move():
    """A window that ends where it started is not a move to explain."""
    t0 = datetime(2026, 8, 17, 9, 15)
    whip = [100.0, 130.0, 100.0, 130.0, 100.0, 130.0, 100.0]
    trend = [100 + 5 * i for i in range(7)]
    assert abs(premove.find_moves(_bars(t0, whip + [100.0] * 8),
                                  30, 5)[0]["move"]) \
        <= abs(premove.find_moves(_bars(t0, trend + [130.0] * 8), 30, 5)[0]["move"])


def test_overlapping_windows_collapse_to_one_move():
    t0 = datetime(2026, 8, 17, 9, 15)
    closes = [100.0] * 6 + [100 + 4 * i for i in range(1, 10)] + [136.0] * 10
    moves = premove.find_moves(_bars(t0, closes), 30, 5, top_n=3)
    for a, b in zip(moves, moves[1:]):
        assert not (a["start"] < b["end"] and b["start"] < a["end"])


def test_no_bars_no_moves():
    assert premove.find_moves([], 30, 5) == []


# --- scoring ----------------------------------------------------------------

def _rows(t0, key, values, step=5):
    return [{"ts": t0 + timedelta(minutes=i * step), key: v}
            for i, v in enumerate(values)]


def test_small_sample_refuses_to_score():
    """A z-score off three points is noise wearing a number's clothes."""
    t0 = datetime(2026, 8, 17, 9, 15)
    rows = _rows(t0, "pcr_oi", [1.0, 1.1, 1.2, 1.3])
    out = premove.score_metric(rows, "pcr_oi", t0, t0 + timedelta(minutes=15))
    assert out["verdict"] == "insufficient"


def test_steady_drift_all_day_is_not_notable():
    """A metric that drifts all session shouldn't score just for drifting."""
    t0 = datetime(2026, 8, 17, 9, 15)
    rows = _rows(t0, "pcr_oi", [1.0 + 0.01 * i for i in range(30)])
    out = premove.score_metric(rows, "pcr_oi", t0 + timedelta(minutes=50),
                               t0 + timedelta(minutes=90))
    assert out["verdict"] in ("ok", "flat-baseline")
    if out["verdict"] == "ok":
        assert not out["notable"]


def test_a_real_acceleration_is_notable():
    t0 = datetime(2026, 8, 17, 9, 15)
    vals = [1.0 + 0.001 * i for i in range(20)] + [1.02 + 0.05 * i for i in range(10)]
    rows = _rows(t0, "pcr_oi", vals)
    out = premove.score_metric(rows, "pcr_oi", rows[20]["ts"], rows[29]["ts"])
    assert out["verdict"] == "ok" and out["notable"]


def test_gaps_are_not_bridged_into_fake_deltas():
    """A missing sample must break the delta chain, not straddle it — else a
    poller outage manufactures one huge bogus 'change'."""
    t0 = datetime(2026, 8, 17, 9, 15)
    rows = _rows(t0, "call_oi", [100.0, 101.0, None, None, 500.0, 501.0])
    ds = premove._deltas(rows, "call_oi")
    assert all(abs(d) < 10 for _, d in ds), ds


# --- end to end -------------------------------------------------------------

def _chain_row(u, ts, soff, otype, oi, strike, iv, spot):
    return (u, ts, None, "MONTHLY", 0, strike, soff, otype, spot,
            100.0, 99.5, 100.5, iv, oi, 500, None, None, None, None)


def test_planted_pre_move_signal_is_surfaced(store):
    """Call OI accelerates hard for 45 min, THEN spot drops. The sweep should
    find the move on its own and flag call OI in the run-up."""
    day = date(2026, 8, 17)
    t0 = datetime(2026, 8, 17, 9, 15)

    closes, px = [], 100.0
    for i in range(48):                 # flat, then a fall starting bar 30
        px = px - 2.0 if i >= 30 else px + (0.05 if i % 2 else -0.05)
        closes.append(px)
    store.bulk_write(
        "INSERT OR REPLACE INTO underlying_bars VALUES (?,?,?,?,?,?,?,?)",
        [("NIFTY", b.ts, b.open, b.high, b.low, b.close, 1000, 0)
         for b in _bars(t0, closes)])

    chain = []
    for i in range(48):
        ts = t0 + timedelta(minutes=5 * i)
        # call OI creeps all morning, then accelerates 45m before bar 30
        coi = 1000.0 + (10 * i if i < 21 else 210 + 400 * (i - 21))
        chain += [_chain_row("NIFTY", ts, 0, "CALL", coi, 100.0, 14.0, closes[i]),
                  _chain_row("NIFTY", ts, 0, "PUT", 900.0, 100.0, 15.0, closes[i]),
                  _chain_row("NIFTY", ts, 1, "CALL", coi / 2, 105.0, 13.0, closes[i]),
                  _chain_row("NIFTY", ts, -1, "PUT", 800.0, 95.0, 16.0, closes[i])]
    store.upsert_chain_rows(chain)

    out = premove.build_report(store, "NIFTY", day, window_min=30,
                               lookback_min=45, sample=5, top_n=1)
    assert "MOVE #1" in out
    assert "call OI" in out
    assert "NOTABLE" in out
    assert "HYPOTHESIS" in out


def test_no_bars_says_so_instead_of_guessing(store):
    out = premove.build_report(store, "NIFTY", date(2026, 8, 17))
    assert "no underlying_bars recorded" in out
