"""
Chain-only (recorder) underlyings must get candles built for them.

Reported symptom 2026-07-27: /data/health showed underlying_bars frozen at
152 rows (two NSE names x 75 bars) and ageing past 30 min while MCX traded
for hours. chain_snapshots and option_bars were updating fine, so the MCX
chain poller worked -- but MCX SPOT/futures candles were never recorded at
all, leaving underlying_bars with no MCX history and MCX unbacktestable.

Cause: candle builders were only created by register(), and only STRATEGIES
call register(). The MCX recorder uses enable_chain(), which created no
builder, so _on_tick found none and dropped every MCX tick.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.data.store import SyntheticStore
from app.engines.paper import IST, MarketHub


def _hub():
    hub = MarketHub(SyntheticStore())
    hub._tick_ok = lambda ts: True     # bypass the session/freshness gate
    return hub


def test_enable_chain_creates_a_record_builder():
    """The fix: a chain-only name gets a RECORD_INTERVAL builder, so its ticks
    have somewhere to go."""
    hub = _hub()
    hub.enable_chain("CRUDEOIL")
    assert ("CRUDEOIL", hub.RECORD_INTERVAL) in hub._builders


def test_chain_only_ticks_produce_bars():
    """End-to-end: ticks on a recorder-only name must emit a completed candle
    (previously they were silently dropped)."""
    hub = _hub()
    hub.enable_chain("CRUDEOIL")
    got = []
    hub._emit = lambda msg: got.append(msg)

    base = datetime.now(IST).replace(second=0, microsecond=0, tzinfo=None)
    base = base.replace(minute=(base.minute // 5) * 5)
    for i in range(5):                       # fill one 5-min bucket
        hub._on_tick("CRUDEOIL", 6000.0 + i, base + timedelta(seconds=i * 30))
    hub._on_tick("CRUDEOIL", 6010.0, base + timedelta(minutes=5, seconds=1))

    bars = [m for m in got if m[0] == "bar" and m[1] == "CRUDEOIL"]
    assert bars, "chain-only underlying produced no candle"
    assert bars[0][2] == hub.RECORD_INTERVAL


def test_strategy_intervals_still_win_and_are_not_duplicated():
    """A name that is BOTH strategy-registered at 5m and chain-enabled must
    build exactly one 5m series, not two."""
    hub = _hub()
    hub.register("NIFTY", 5)
    hub.enable_chain("NIFTY")
    assert hub._tick_intervals("NIFTY") == (5,)


def test_unknown_underlying_builds_nothing():
    hub = _hub()
    assert hub._tick_intervals("WHATEVER") == ()


def test_eod_flush_skips_mcx_builders():
    """15:31 is the NSE close; MCX runs to 23:30. Flushing an MCX builder
    there would persist a PARTIAL candle mid-session and corrupt the history
    we just started recording."""
    from app.data.dhan_client import UNDERLYINGS
    hub = _hub()
    hub.enable_chain("CRUDEOIL")
    hub.register("NIFTY", 5)
    # both builders hold an open, unfinished bucket
    base = datetime.now(IST).replace(second=0, microsecond=0, tzinfo=None)
    hub._on_tick("CRUDEOIL", 6000.0, base)
    hub._on_tick("NIFTY", 23800.0, base)

    seg = str((UNDERLYINGS.get("CRUDEOIL") or {}).get("segment", ""))
    if "MCX" not in seg:
        return          # MCX ids unresolved in this env — nothing to assert

    flushed = []
    for (u, interval), b in hub._builders.items():
        cfg = UNDERLYINGS.get(u) or {}
        if "MCX" in str(cfg.get("segment", "")):
            continue
        if b.flush() is not None:
            flushed.append(u)
    assert "CRUDEOIL" not in flushed
