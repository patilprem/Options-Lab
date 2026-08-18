"""Signal validation harness — the anti-overfit machinery.

Offline. The tests that matter here are the NEGATIVE ones: a harness that
reports edge where none exists is worse than no harness, because it launders
noise into confidence. So these check that random data yields ~zero edge, that
a small sample refuses to produce a verdict, that gaps don't manufacture
firings, and that a genuinely planted effect IS detected.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from app.engines import signal_study as S


def _series(n, skews, biases, spots, t0=datetime(2026, 8, 17, 9, 15)):
    return [{"ts": t0 + timedelta(minutes=5 * i), "skew": skews[i],
             "bias": biases[i], "spot": spots[i]} for i in range(n)]


def test_no_firings_when_bias_agrees():
    n = 40
    s = _series(n, [-1.0 - 0.1 * i for i in range(n)], [+0.9] * n, [100.0] * n)
    assert S.firings(s, 9, 0.5, 0.3) == []


def test_fires_on_divergence():
    n = 40
    skews = [-1.0] * 20 + [-1.0 - 0.15 * i for i in range(20)]
    s = _series(n, skews, [-0.9] * n, [100.0] * n)
    f = S.firings(s, 9, 0.5, 0.3)
    assert f and all(x["side"] == "bullish" for x in f)


def test_gap_inside_lookback_blocks_the_firing():
    """A poller outage must not manufacture an enormous fake shift."""
    n = 40
    skews = [-1.0] * 20 + [-5.0] * 20
    skews[25] = None
    s = _series(n, skews, [-0.9] * n, [100.0] * n)
    for f in S.firings(s, 9, 0.5, 0.3):
        assert f["ts"] != s[26]["ts"]


def test_truncated_horizon_is_none_not_zero():
    s = _series(5, [-1.0] * 5, [-0.9] * 5, [100.0] * 5)
    fwd = S.forward_returns(s, 3, (15, 60))
    assert fwd[60] is None


def test_small_sample_refuses_a_verdict():
    n = 40
    skews = [-1.0] * 20 + [-1.0 - 0.15 * i for i in range(20)]
    s = _series(n, skews, [-0.9] * n, [100.0 + i for i in range(n)])
    f = S.firings(s, 9, 0.5, 0.3, (15,))
    summ = S.summarize(f, S.baseline(s, (15,)), (15,))
    assert summ["bullish"][15]["verdict"] == "insufficient"


def test_random_data_shows_no_meaningful_edge():
    """The most important test: noise in, no edge out."""
    rng = random.Random(7)
    days = []
    for d in range(60):
        n = 70
        skews = [rng.gauss(0, 1) for _ in range(n)]
        biases = [rng.gauss(0, 0.5) for _ in range(n)]
        spots, px = [], 100.0
        for _ in range(n):
            px += rng.gauss(0, 1.0)
            spots.append(px)
        days.append((d, _series(n, skews, biases, spots)))
    r = S.study(days, 9, 0.5, 0.3, (30,))
    row = r["summary"]["bullish"][30]
    if row["verdict"] == "ok":
        assert abs(row["z"]) < 3.0, f"spurious edge on random data: {row}"


def test_planted_effect_is_detected():
    """When the condition genuinely precedes a rise, edge must be positive."""
    rng = random.Random(11)
    days = []
    for d in range(60):
        n = 70
        skews, biases, spots = [], [], []
        px = 100.0
        for i in range(n):
            firing = 20 <= i < 24
            skews.append(-1.0 - (0.3 * (i - 19) if firing else 0.0)
                         + rng.gauss(0, 0.02))
            biases.append(-0.9)
            # price rises hard only AFTER the firing window
            px += (3.0 if 24 <= i < 34 else rng.gauss(0, 0.2))
            spots.append(px)
        days.append((d, _series(n, skews, biases, spots)))
    r = S.study(days, 9, 0.5, 0.3, (30,))
    row = r["summary"]["bullish"][30]
    assert row["verdict"] == "ok"
    assert row["edge"] > 0, row


def test_sweep_covers_the_grid():
    days = [(0, _series(40, [-1.0] * 40, [-0.9] * 40, [100.0] * 40))]
    rows = S.sweep(days, [6, 9], [0.3, 0.5], [0.3], 30)
    assert len(rows) == 2 * 2 * 1 * 2      # lb x shift x gate x side
