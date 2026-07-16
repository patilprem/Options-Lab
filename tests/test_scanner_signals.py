"""F6 tests: ctx.signal() exposure across contexts, the signal provider bridge,
and forward-return validation stats."""

from __future__ import annotations

from app.engines import scanner, signals


class _FakeScanner:
    """Minimal provider standing in for the scanner engine."""
    def __init__(self):
        self.index_bias = {"NIFTY": {"score": 0.5, "label": "bullish"}}
        self.scores = {"RELIANCE": {"score": 72, "bias": "CE"}}
        self.metrics = {"RELIANCE": {"buildup": "long_buildup"}}
        self.tier2 = {"RELIANCE": {"pcr_oi": 0.8}}

    def signal_for(self, underlying, name):
        return scanner.StockScanner.signal_for(self, underlying, name)


def test_signal_provider_routes_names():
    signals.register(_FakeScanner())
    try:
        assert signals.get_signal("NIFTY", "index_bias")["label"] == "bullish"
        assert signals.get_signal("RELIANCE", "setup")["bias"] == "CE"
        assert signals.get_signal("RELIANCE", "tier1")["buildup"] == "long_buildup"
        assert signals.get_signal("RELIANCE", "tier2")["pcr_oi"] == 0.8
        assert signals.get_signal("RELIANCE", "unknown_name") is None
        assert signals.get_signal("MISSING", "setup") is None
    finally:
        signals.register(None)


def test_get_signal_no_provider_is_none():
    signals.register(None)
    assert signals.get_signal("NIFTY", "index_bias") is None


def test_get_signal_never_raises():
    class _Boom:
        def signal_for(self, u, n):
            raise RuntimeError("boom")
    signals.register(_Boom())
    try:
        assert signals.get_signal("NIFTY", "index_bias") is None
    finally:
        signals.register(None)


def test_backtest_context_signal_is_none():
    # A backtest must never see scanner signals even if a provider is set.
    from app.engines.backtest import BacktestContext
    signals.register(_FakeScanner())
    try:
        ctx = BacktestContext.__new__(BacktestContext)   # signal() ignores state
        ctx.underlying = "NIFTY"
        assert ctx.signal("index_bias") is None
    finally:
        signals.register(None)


def test_smoke_context_signal_is_none():
    from app.core.loader import _SmokeContext
    assert _SmokeContext().signal("index_bias") is None


# --- validation stats -------------------------------------------------------

def test_hitrate_stats_overall_and_buckets():
    rows = [
        {"score": 80, "entry": 10.0, "exit": 13.0},   # +30% hit
        {"score": 75, "entry": 10.0, "exit": 8.0},    # -20% miss
        {"score": 60, "entry": 5.0, "exit": 6.0},     # +20% hit
        {"score": 30, "entry": 4.0, "exit": 3.0},     # miss, low band
        {"score": 90, "entry": 0.0, "exit": 5.0},     # zero entry -> dropped
        {"score": 90, "entry": 5.0, "exit": None},    # no exit -> dropped
    ]
    s = scanner.hitrate_stats(rows)
    assert s["overall"]["n"] == 4
    assert s["overall"]["hits"] == 2
    assert s["overall"]["hit_rate"] == 0.5
    assert s["by_score"]["70-100"]["n"] == 2
    assert s["by_score"]["70-100"]["hits"] == 1
    assert s["by_score"]["<40"]["n"] == 1


def test_hitrate_stats_empty():
    s = scanner.hitrate_stats([])
    assert s["overall"]["n"] == 0 and s["overall"]["hit_rate"] is None
