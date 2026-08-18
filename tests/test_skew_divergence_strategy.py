"""examples/skew_bias_divergence.py — the signal gate.

Offline. The strategy trades a DISAGREEMENT between iv_skew and index_bias,
so the tests that matter are the ones proving it stays OUT: when the two
agree, when either reading is missing, when price has already run, and when a
position is already open (instance state does not survive a restart, but
positions do — contract rule 8).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.core import contract as C
from app.core.loader import load_strategy_class

SRC = Path(__file__).resolve().parents[1] / "examples" / "skew_bias_divergence.py"


class FakeCtx(C.Context):
    def __init__(self, skew_series, bias_score, spot=100.0, day_lo=95.0,
                 day_hi=105.0, positions=()):
        self._skews = list(skew_series)
        self._bias = bias_score
        self._spot = spot
        self._lo, self._hi = day_lo, day_hi
        self._positions = list(positions)
        self._now = datetime(2026, 8, 17, 9, 20)
        self.entered = []
        self.logs = []

    # -- market data
    @property
    def now(self): return self._now
    @property
    def spot(self): return self._spot

    def option(self, leg): return None

    def history(self, n, interval=None):
        base = datetime(2026, 8, 17, 9, 15)
        out = []
        for i in range(12):
            hi = self._hi if i == 0 else self._spot
            lo = self._lo if i == 0 else self._spot
            out.append(C.Bar(base + timedelta(minutes=5 * i), self._spot,
                             hi, lo, self._spot, 100))
        return out

    def chain(self):
        return None if self._skews[0] is None else {"iv_skew": self._skews[0]}

    def signal(self, name):
        if name != "index_bias" or self._bias is None:
            return None
        return {"score": self._bias, "label": "x"}

    # -- portfolio
    @property
    def positions(self): return self._positions
    @property
    def allocated_capital(self): return 1_000_000.0
    @property
    def available_capital(self): return 1_000_000.0
    @property
    def day_pnl(self): return 0.0

    # -- actions
    def enter(self, legs, tag="", sl_pct=None, target_pct=None):
        self.entered.append((legs[0].option_type, tag))
        return True

    def set_levels(self, position_id, stop_loss=None, target=None): return True
    def exit(self, position_id, reason="signal"): return True
    def exit_all(self, reason="signal"): self._positions = []
    def log(self, msg): self.logs.append(msg)


def _strategy():
    return load_strategy_class(SRC.read_text())()


def _run(skews, bias, spot=96.0, positions=(), at=(10, 30)):
    """Feed a skew series bar by bar; return the ctx after the last bar."""
    s = _strategy()
    ctx = FakeCtx(skews, bias, spot=spot, positions=positions)
    for i, sk in enumerate(skews):
        ctx._skews = [sk]
        ctx._now = datetime(2026, 8, 17, at[0], at[1]) + timedelta(minutes=5 * i)
        s.on_bar(ctx, C.Bar(ctx._now, spot, spot, spot, spot, 100))
    return ctx


# 10 bars: flat, then a hard fall over the last few -> shift < -0.5
FALLING = [-1.0] * 10 + [-1.2, -1.5, -1.8]
RISING = [1.0] * 10 + [1.2, 1.5, 1.8]


def test_falling_skew_plus_bearish_bias_buys_a_call():
    """The 2026-08-17 setup: calls richening while breadth says bearish."""
    ctx = _run(FALLING, bias=-0.58)
    assert ctx.entered, "expected an entry"
    assert ctx.entered[0][0] == C.OptionType.CALL


def test_rising_skew_plus_bullish_bias_buys_a_put():
    ctx = _run(RISING, bias=+0.58, spot=104.0)
    assert ctx.entered
    assert ctx.entered[0][0] == C.OptionType.PUT


def test_agreement_does_not_trade():
    """Falling skew with BEARISH-agreeing... i.e. bullish bias = no divergence.
    This is the whole point: agreement is not a signal."""
    assert not _run(FALLING, bias=+0.58).entered


def test_weak_bias_does_not_trade():
    """|bias| below the gate is not a disagreement, just noise."""
    assert not _run(FALLING, bias=-0.05).entered


def test_missing_bias_does_not_trade():
    assert not _run(FALLING, bias=None).entered


def test_missing_chain_does_not_trade():
    assert not _run([None] * 13, bias=-0.58).entered


def test_small_skew_shift_does_not_trade():
    assert not _run([-1.0] * 10 + [-1.05, -1.1, -1.15], bias=-0.58).entered


def test_does_not_chase_a_call_after_price_has_run():
    """Signal fires but spot is at the TOP of the day's range — buying the
    turn is the thesis, chasing it is not."""
    assert not _run(FALLING, bias=-0.58, spot=104.9).entered


def test_never_enters_while_a_position_is_open():
    """Contract rule 8: after a restart self._trades_today is 0 again while
    the position survives. ctx.positions is the guard that must hold."""
    pos = [object()]
    assert not _run(FALLING, bias=-0.58, positions=pos).entered


def test_one_trade_per_day():
    s = _strategy()
    ctx = FakeCtx([-1.0], -0.58, spot=96.0)
    for i, sk in enumerate(FALLING + FALLING):
        ctx._skews = [sk]
        ctx._now = datetime(2026, 8, 17, 10, 30) + timedelta(minutes=5 * i)
        s.on_bar(ctx, C.Bar(ctx._now, 96.0, 96.0, 96.0, 96.0, 100))
        ctx._positions = []          # pretend it closed immediately
    assert len(ctx.entered) == 1


def test_outside_the_entry_window_does_not_trade():
    assert not _run(FALLING, bias=-0.58, at=(14, 5)).entered
