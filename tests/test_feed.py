"""Offline tests for the live-feed candle building and MarketHub tick wiring.
No network — the SDK MarketFeed is only imported inside LiveFeed's thread."""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.core.contract import Bar
from app.engines.feed import CandleBuilder
from app.engines.paper import MarketHub


# --- CandleBuilder ----------------------------------------------------------

def _dt(h, m, s=0):
    return datetime(2026, 7, 9, h, m, s)


def test_bucket_alignment():
    cb = CandleBuilder(5)
    assert cb._bucket_start(_dt(9, 16, 30)) == _dt(9, 15)
    assert cb._bucket_start(_dt(9, 20, 0)) == _dt(9, 20)
    assert cb._bucket_start(_dt(9, 24, 59)) == _dt(9, 20)


def test_no_bar_until_bucket_rolls():
    cb = CandleBuilder(5)
    assert cb.add_tick(_dt(9, 16), 100.0) is None   # opens 09:15 bucket
    assert cb.add_tick(_dt(9, 17), 105.0) is None
    assert cb.add_tick(_dt(9, 18), 98.0) is None


def test_completed_bar_ohlc_and_label():
    cb = CandleBuilder(5)
    cb.add_tick(_dt(9, 16), 100.0)
    cb.add_tick(_dt(9, 17), 105.0)
    cb.add_tick(_dt(9, 18), 98.0)
    bar = cb.add_tick(_dt(9, 21), 102.0)  # first tick of the 09:20 bucket
    assert isinstance(bar, Bar)
    assert bar.ts == _dt(9, 15)   # labeled at bucket start
    # close is the last tick INSIDE the 09:15 bucket (98.0 @ 09:18), 102 opens next
    assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 105.0, 98.0, 98.0)


def test_flush_emits_partial():
    cb = CandleBuilder(5)
    cb.add_tick(_dt(9, 16), 100.0)
    cb.add_tick(_dt(9, 17), 110.0)
    bar = cb.flush()
    assert bar.ts == _dt(9, 15) and bar.high == 110.0
    assert cb.flush() is None  # nothing left after flush


def test_volume_accumulates_within_bucket():
    cb = CandleBuilder(5)
    cb.add_tick(_dt(9, 16), 100.0, volume=10)
    cb.add_tick(_dt(9, 17), 101.0, volume=15)
    bar = cb.add_tick(_dt(9, 20), 102.0)
    assert bar.volume == 25


# --- MarketHub tick wiring --------------------------------------------------

class _FakeStore:
    """Not SyntheticStore, so _use_synthetic falls through to the creds check."""
    def option_close(self, *a, **k):
        return None


def test_register_and_instrument_mapping():
    hub = MarketHub(_FakeStore())
    hub.register("NIFTY", 5)
    hub.register("NIFTY", 15)   # second timeframe, same underlying
    hub.register("BANKNIFTY", 5)
    assert hub._wanted == {"NIFTY": {5, 15}, "BANKNIFTY": {5}}
    # spot index instruments: (IDX=0, security_id, Ticker=15)
    insts = dict((sid, (seg, mode)) for seg, sid, mode in hub._instruments())
    assert insts["13"] == (0, 15)   # NIFTY security_id 13
    assert insts["25"] == (0, 15)   # BANKNIFTY security_id 25
    assert hub._sec_to_underlying()[13] == "NIFTY"


def test_on_tick_emits_completed_bars_per_interval():
    hub = MarketHub(_FakeStore())
    hub.register("NIFTY", 5)
    q = hub.subscribe()

    hub._on_tick("NIFTY", 100.0, _dt(9, 16))
    hub._on_tick("NIFTY", 105.0, _dt(9, 17))
    assert q.empty()                       # still inside the 09:15 bucket
    hub._on_tick("NIFTY", 102.0, _dt(9, 21))   # rolls -> emits 09:15 bar

    kind, underlying, interval, bar = q.get_nowait()
    assert (kind, underlying, interval) == ("bar", "NIFTY", 5)
    assert bar.ts == _dt(9, 15)
    assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 105.0, 100.0, 105.0)
    # ticks for an unregistered underlying are ignored
    hub._on_tick("SENSEX", 200.0, _dt(9, 22))
    assert q.empty()


def test_use_synthetic_env_flag(monkeypatch):
    monkeypatch.setenv("OPTIONSLAB_SYNTHETIC", "1")
    assert MarketHub(_FakeStore())._use_synthetic() is True


def test_use_synthetic_true_for_synthetic_store():
    from app.data.store import SyntheticStore
    assert MarketHub(SyntheticStore())._use_synthetic() is True
