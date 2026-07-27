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
    hub._tick_ok = lambda ts, u="": True   # bypass the session/freshness gate
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


# --- the tick gate itself ---------------------------------------------------
# The builder fix above was necessary but NOT sufficient: _tick_ok hardcoded
# NSE's 09:15-15:30 window for EVERY underlying, so MCX ticks outside it were
# discarded before reaching any builder. MCX trades 09:00-23:30, so most of
# its session was thrown away. underlying_bars stayed frozen at 152 rows even
# after builders existed -- two independent layers had to be fixed.

from datetime import time as dtime           # noqa: E402


def _mcx_is_resolved() -> bool:
    from app.data.dhan_client import UNDERLYINGS
    return "MCX" in str((UNDERLYINGS.get("CRUDEOIL") or {}).get("segment", ""))


def _at(hh, mm):
    """A weekday timestamp at hh:mm, fresh relative to the freshness check."""
    base = datetime.now(IST).replace(tzinfo=None)
    while base.weekday() >= 5:
        base -= timedelta(days=1)
    return base.replace(hour=hh, minute=mm, second=0, microsecond=0)


def test_nse_window_still_rejects_out_of_session_ticks():
    hub = MarketHub(SyntheticStore())
    hub.TICK_FRESHNESS_S = None              # isolate the window check
    assert hub._tick_ok(_at(10, 0), "NIFTY") is True
    assert hub._tick_ok(_at(9, 5), "NIFTY") is False      # pre-open auction
    assert hub._tick_ok(_at(16, 0), "NIFTY") is False     # after NSE close


def test_mcx_evening_ticks_are_accepted():
    """The regression: MCX ticks after the NSE close must survive the gate."""
    if not _mcx_is_resolved():
        return                                # MCX ids unresolved in this env
    hub = MarketHub(SyntheticStore())
    hub.TICK_FRESHNESS_S = None
    assert hub._tick_ok(_at(9, 5), "CRUDEOIL") is True    # MCX opens 09:00
    assert hub._tick_ok(_at(16, 0), "CRUDEOIL") is True   # was rejected
    assert hub._tick_ok(_at(22, 30), "CRUDEOIL") is True  # was rejected
    assert hub._tick_ok(_at(23, 45), "CRUDEOIL") is False  # past MCX close


def test_unknown_underlying_keeps_the_stricter_nse_window():
    hub = MarketHub(SyntheticStore())
    hub.TICK_FRESHNESS_S = None
    assert hub._tick_ok(_at(16, 0), "WHATEVER") is False


def test_weekend_ticks_always_rejected():
    hub = MarketHub(SyntheticStore())
    hub.TICK_FRESHNESS_S = None
    sat = _at(11, 0)
    while sat.weekday() != 5:
        sat += timedelta(days=1)
    assert hub._tick_ok(sat, "CRUDEOIL") is False
    assert hub._tick_ok(sat, "NIFTY") is False
