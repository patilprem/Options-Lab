"""Move anatomy — discovery harness for pre-move behaviour.

Offline. The load-bearing tests are the negative ones: a harness that reports
a tradeable precursor where none exists launders noise into confidence. So
these check that a feature which fires before every move BUT ALSO everywhere
else scores ~1.0x lift (the high-recall trap), that random data produces no
large effect size, that gaps never become features, and that a genuinely
discriminating feature IS found.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from app.engines import move_anatomy as M


def _series(n, skews, spots, biases=None, t0=datetime(2026, 8, 17, 9, 15)):
    biases = biases or [-0.5] * n
    return [{"ts": t0 + timedelta(minutes=5 * i), "spot": spots[i],
             "high": spots[i] + 1, "low": spots[i] - 1, "bias": biases[i],
             "pcr_oi": 1.0, "atm_iv": 12.0, "iv_skew": skews[i],
             "call_oi": 1000.0, "put_oi": 900.0, "max_pain": spots[i]}
            for i in range(n)]


def test_label_requires_both_atr_and_pct():
    """A dead-quiet day must not promote noise to a 'move' via collapsed ATR."""
    n = 40
    spots = [100.0 + 0.01 * i for i in range(n)]     # tiny drift
    s = _series(n, [-1.0] * n, spots)
    assert M.label_at(s, 10, 6, 2.0, 0.35) == "quiet"


def test_label_finds_a_real_move():
    n = 40
    spots = [100.0] * 20 + [100.0 + 1.5 * i for i in range(20)]
    s = _series(n, [-1.0] * n, spots)
    assert M.label_at(s, 20, 6, 2.0, 0.35) == "up"


def test_gap_in_lookback_yields_no_features():
    n = 40
    skews = [-1.0] * n
    skews[15] = None
    s = _series(n, skews, [100.0] * n)
    assert M.features_at(s, 20, 9) is None


def test_high_recall_but_useless_feature_scores_no_lift():
    """THE TRAP: a feature present before every move and before everything
    else. Recall ~100%, lift ~1.0x. It must not look tradeable."""
    rng = random.Random(3)
    days = []
    for d in range(40):
        n = 70
        spots, px = [], 100.0
        for i in range(n):
            px += (2.0 if 30 <= i < 40 else rng.gauss(0, 0.3))
            spots.append(px)
        # skew is ALWAYS falling — before moves and before quiet alike
        skews = [-1.0 - 0.1 * i for i in range(n)]
        days.append((d, _series(n, skews, spots)))
    samples = M.collect(days, 9, 6, 2.0, 0.35)
    pr = M.precision_recall(samples, "d_skew", "up", -1)
    if pr["verdict"] == "ok" and pr["best"]:
        assert pr["best"]["lift"] < 1.5, pr


def test_random_data_has_no_large_effect_size():
    rng = random.Random(5)
    days = []
    for d in range(40):
        n = 70
        spots, px = [], 100.0
        for _ in range(n):
            px += rng.gauss(0, 1.0)
            spots.append(px)
        days.append((d, _series(n, [rng.gauss(0, 1) for _ in range(n)], spots)))
    samples = M.collect(days, 9, 6, 2.0, 0.35)
    sep = M.separation(samples, "d_skew", "up")
    if sep["verdict"] == "ok":
        assert abs(sep["d"]) < 0.5, sep


def test_a_genuinely_discriminating_feature_is_found():
    """Skew falls ONLY before up-moves. Effect size and lift must both show."""
    rng = random.Random(9)
    days = []
    for d in range(40):
        n = 70
        spots, skews, px, sk = [], [], 100.0, -1.0
        for i in range(n):
            # skew falls through the run-up and HOLDS there — it must not snap
            # back, or d_skew flips positive across the very bars being tested
            if 21 <= i < 30:
                sk -= 0.4
            skews.append(sk + rng.gauss(0, 0.02))
            px += (2.0 if 30 <= i < 40 else rng.gauss(0, 0.2))
            spots.append(px)
        days.append((d, _series(n, skews, spots)))
    samples = M.collect(days, 9, 6, 2.0, 0.35)
    sep = M.separation(samples, "d_skew", "up")
    assert sep["verdict"] == "ok"
    assert abs(sep["d"]) > 0.5, sep
    pr = M.precision_recall(samples, "d_skew", "up", -1 if sep["d"] < 0 else 1)
    assert pr["verdict"] == "ok" and pr["best"]["lift"] > 1.2, pr


def test_too_few_moves_refuses_a_verdict():
    days = [(0, _series(40, [-1.0] * 40, [100.0] * 40))]
    samples = M.collect(days, 9, 6, 2.0, 0.35)
    assert M.separation(samples, "d_skew", "up")["verdict"] == "insufficient"


# --- independent events + holdout (the overlap correction) ------------------

def test_consecutive_bars_collapse_to_one_event():
    """Six consecutive 5-min samples share almost all of their window — one
    event observed six times, not six observations."""
    t0 = datetime(2026, 8, 17, 10, 0)
    items = [("d1", t0 + timedelta(minutes=5 * i)) for i in range(6)]
    assert M.count_events(items, gap_bars=6, sample=5) == 1


def test_separated_bars_are_separate_events():
    t0 = datetime(2026, 8, 17, 10, 0)
    items = [("d1", t0), ("d1", t0 + timedelta(minutes=120))]
    assert M.count_events(items, gap_bars=6, sample=5) == 2


def test_same_time_different_days_are_separate_events():
    t0 = datetime(2026, 8, 17, 10, 0)
    assert M.count_events([("d1", t0), ("d2", t0)], gap_bars=6, sample=5) == 2


def test_verdict_gates_on_events_not_bars():
    """THE correction: a run of overlapping bars must not buy a verdict.
    One long continuous firing is one event however many bars it spans."""
    n = 200
    # skew falls continuously; price rises continuously -> every bar is 'up'
    days = [("d1", _series(n, [-1.0 - 0.05 * i for i in range(n)],
                           [100.0 + 2.0 * i for i in range(n)]))]
    samples = M.collect(days, 9, 6, 2.0, 0.35)
    sep = M.separation(samples, "d_skew", "up")
    assert sep["verdict"] == "insufficient", sep
    assert sep["n_events"] < M.MIN_CLASS


def test_split_days_is_chronological_never_shuffled():
    """Shuffling would leak tomorrow into today."""
    days = [(date(2026, 8, d), []) for d in range(1, 11)]
    disc, hold = M.split_days(days, 0.4)
    assert [d for d, _ in disc] == [date(2026, 8, d) for d in range(1, 7)]
    assert [d for d, _ in hold] == [date(2026, 8, d) for d in range(7, 11)]
    assert max(d for d, _ in disc) < min(d for d, _ in hold)
