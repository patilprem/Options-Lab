"""
Signal attribution (step 6)
===========================
Pure bucketing + entry-context capture + the backtest report wiring. Offline.
"""

from __future__ import annotations

from datetime import datetime

from app.core.contract import (Action, ExpiryKind, LegSpec, OptionType,
                               StrategyMeta)
from app.data.store import SyntheticStore
from app.engines import backtest as bt
from app.engines.attribution import attribution, capture_entry_context


# --- attribution() ----------------------------------------------------------

def _t(iv, pnl):
    return {"entry_context": {"iv_rank": iv}, "pnl": pnl}


def test_attribution_numeric_bins():
    trades = [_t(20, 100), _t(25, -50), _t(80, 300), _t(90, 400), _t(None, 10)]
    a = attribution(trades, "iv_rank", bins=[0, 30, 70, 100])
    assert a["0-30"]["n"] == 2 and a["0-30"]["wins"] == 1
    assert a["0-30"]["win_rate"] == 50.0
    assert ">=100" not in a                                # nothing at/above 100
    assert a["70-100"]["n"] == 2 and a["70-100"]["win_rate"] == 100.0
    assert a["70-100"]["avg_pnl"] == 350.0
    assert a["unknown"]["n"] == 1
    assert a["overall"]["n"] == 5


def test_attribution_below_first_edge_bucket():
    a = attribution([{"entry_context": {"index_bias": -0.8}, "pnl": 5}],
                    "index_bias", bins=[-0.3, 0.3])
    assert "<-0.3" in a and a["<-0.3"]["n"] == 1


def test_attribution_categorical():
    trades = [{"entry_context": {"tod": "09:20"}, "pnl": 10},
              {"entry_context": {"tod": "09:20"}, "pnl": -5},
              {"entry_context": {"tod": "14:00"}, "pnl": 20}]
    a = attribution(trades, "tod")
    assert a["09:20"]["n"] == 2 and a["14:00"]["n"] == 1
    assert a["14:00"]["win_rate"] == 100.0


# --- capture_entry_context() ------------------------------------------------

class _Ctx:
    def __init__(self, now, ivr=None, bias=None, chain=None, raise_iv=False):
        self._now, self._ivr, self._bias = now, ivr, bias
        self._chain, self._raise_iv = chain, raise_iv

    @property
    def now(self):
        return self._now

    def iv_rank(self, lookback_days=30):
        if self._raise_iv:
            raise RuntimeError("boom")
        return self._ivr

    def signal(self, name):
        return self._bias if name == "index_bias" else None

    def chain(self):
        return self._chain


def test_capture_full():
    c = _Ctx(datetime(2026, 7, 16, 9, 20), ivr=62.34,
             bias={"score": 0.5},
             chain={"pcr_oi": 1.234, "atm_iv": 15.0, "iv_skew": 0.31,
                    "max_pain": 22000})
    snap = capture_entry_context(c)
    assert snap["tod"] == "09:20"
    assert snap["iv_rank"] == 62.3
    assert snap["index_bias"] == 0.5
    assert snap["pcr_oi"] == 1.234 and snap["max_pain"] == 22000


def test_capture_is_defensive():
    # a failing read is omitted, the rest still captured
    c = _Ctx(datetime(2026, 7, 16, 9, 20), raise_iv=True, bias={"score": 0.2})
    snap = capture_entry_context(c)
    assert "iv_rank" not in snap
    assert snap["index_bias"] == 0.2 and snap["tod"] == "09:20"


def test_capture_thin_when_unknown():
    c = _Ctx(datetime(2026, 7, 16, 9, 20))    # no iv/bias/chain
    snap = capture_entry_context(c)
    assert snap == {"tod": "09:20"}


# --- backtest report wiring -------------------------------------------------

class _OneShot(bt.Strategy):
    def __init__(self):
        self.params = {}
        self.done = None

    def meta(self):
        return StrategyMeta(name="oneshot", underlying="NIFTY", timeframe="5",
                            params={})

    def on_bar(self, ctx, bar):
        t = ctx.now.time()
        if self.done == ctx.now.date() or ctx.positions:
            return
        if (t.hour, t.minute) >= (10, 0):
            ok = ctx.enter([LegSpec(OptionType.CALL, Action.SELL, 0,
                                    ExpiryKind.WEEKLY, 0, 1)], tag="x", sl_pct=0.5)
            if ok:
                self.done = ctx.now.date()


def test_backtest_report_has_trades_and_attribution():
    store = SyntheticStore()
    start = datetime(2026, 7, 16, 9, 15)
    end = datetime(2026, 7, 16, 15, 30)
    res = bt.run_backtest(_OneShot(), store, start, end, 1_000_000)
    assert res["trades"], "expected at least one closed trade"
    for tr in res["trades"]:
        assert "tod" in tr["entry_context"]      # captured at entry
    assert "attribution" in res
    n = len(res["trades"])
    # synthetic store has no recorded IV -> all trades in the 'unknown' bucket
    assert res["attribution"]["iv_rank"]["unknown"]["n"] == n
    assert res["attribution"]["iv_rank"]["overall"]["n"] == n
