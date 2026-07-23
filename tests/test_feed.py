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
    hub.TICK_FRESHNESS_S = None      # fixed historical clock in this test
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


def test_tick_gate_drops_offsession_and_stale_ticks():
    """Pre-open auction prints, weekend snapshot ticks and future/stale-stamped
    ticks must never build candles (they produced 09:00 bars, weekend bars and
    a future bar that tripped the 15:25 EOD branch mid-day on 2026-07-13)."""
    from datetime import datetime as _d
    hub = MarketHub(_FakeStore())
    hub.TICK_FRESHNESS_S = None          # isolate the session-window checks
    hub.register("NIFTY", 5)
    q = hub.subscribe()
    hub._on_tick("NIFTY", 100.0, _d(2026, 7, 9, 9, 0))    # pre-open auction
    hub._on_tick("NIFTY", 100.0, _d(2026, 7, 9, 15, 45))  # after close
    hub._on_tick("NIFTY", 100.0, _d(2026, 7, 12, 11, 0))  # Sunday
    hub._on_tick("NIFTY", 100.0, _d(2026, 7, 9, 9, 16))   # ok - opens bucket
    hub._on_tick("NIFTY", 101.0, _d(2026, 7, 9, 9, 21))   # rolls -> 1 bar
    kind, u, iv, bar = q.get_nowait()
    assert bar.ts == _d(2026, 7, 9, 9, 15)
    assert bar.open == 100.0                # junk ticks never entered the bar
    assert q.empty()

    # wall-clock freshness: a tick stamped far from 'now' is dropped
    hub2 = MarketHub(_FakeStore())
    hub2.register("NIFTY", 5)
    q2 = hub2.subscribe()
    hub2._on_tick("NIFTY", 100.0, _d(2026, 7, 9, 12, 0))  # in-session but stale vs now
    hub2._on_tick("NIFTY", 100.0, _d(2026, 7, 9, 12, 6))
    assert q2.empty()


def test_use_synthetic_env_flag(monkeypatch):
    monkeypatch.setenv("OPTIONSLAB_SYNTHETIC", "1")
    assert MarketHub(_FakeStore())._use_synthetic() is True


def test_use_synthetic_true_for_synthetic_store():
    from app.data.store import SyntheticStore
    assert MarketHub(SyntheticStore())._use_synthetic() is True


# --- feed status surface (dashboard Feed pill) -------------------------------

def test_hub_feed_status_off_then_synthetic(tmp_path, monkeypatch):
    from app.core import registry
    from app.data.store import SyntheticStore
    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "t.db")
    registry.init_db()
    hub = MarketHub(SyntheticStore())
    st = hub.feed_status()
    assert st == {"mode": "off", "connected": False, "last_tick": None,
                  "tick_age_sec": None}

    async def run():
        await hub.ensure_started()
        st = hub.feed_status()
        assert st["mode"] == "synthetic" and st["connected"] is True
        await hub.stop()
    asyncio.run(run())


def test_instruments_include_mcx_chain_names_segment_aware(tmp_path, monkeypatch):
    """Chain-only MCX names ride the WS as commodity-segment instruments
    (the 09:00 feed canary); index underlyings stay on the IDX segment."""
    from app.core import registry
    from app.data.store import SyntheticStore
    from app.data import dhan_client as dc
    from app.engines import paper as P

    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "t.db")
    registry.init_db()
    monkeypatch.setitem(dc.UNDERLYINGS, "CRUDEOIL",
                        {"security_id": 428414, "segment": "MCX_COMM",
                         "fno_segment": "MCX_COMM", "instrument": "OPTFUT"})
    hub = P.MarketHub(SyntheticStore())
    hub.register("NIFTY", 5)
    hub.enable_chain("CRUDEOIL")
    inst = sorted(hub._instruments())
    assert (P._FEED_IDX, "13", P._FEED_TICKER) in inst
    assert (P._FEED_MCX, "428414", P._FEED_TICKER) in inst
    assert hub._sec_to_underlying()[428414] == "CRUDEOIL"
    assert hub._watch_segments() == {"NSE", "MCX"}


def test_stalled_socket_forces_reconnect(monkeypatch):
    """Zombie-socket regression (2026-07-15): connected=true but no packets
    for 3h during MCX hours. A read that produces nothing for STALL_S during
    market hours must tear down and reconnect, not wait forever."""
    import sys
    import types
    from app.engines.feed import LiveFeed

    connects = []

    class FakeFeed:
        def __init__(self, ctx, instruments, version):
            connects.append(instruments)

        async def connect(self):
            return None

        async def get_instrument_data(self):
            await asyncio.sleep(3600)   # silent forever — the zombie

        def _is_ws_closed(self):
            return False

        async def disconnect(self):
            return None

    monkeypatch.setitem(sys.modules, "dhanhq",
                        types.SimpleNamespace(MarketFeed=FakeFeed))

    events = []
    lf = LiveFeed(context_factory=lambda: None,
                  instruments=lambda: [(0, "13", 15)],
                  sec_to_underlying=lambda: {13: "NIFTY"},
                  on_tick=lambda *a: None,
                  on_event=lambda lvl, msg: events.append((lvl, msg)),
                  watch_open=lambda: True)          # market open -> stall = dead
    lf.STALL_S = 0.05
    lf._running = True

    async def run():
        task = asyncio.get_running_loop().create_task(lf._driver())
        for _ in range(200):                        # wait for 2nd connect
            if len(connects) >= 2:
                break
            await asyncio.sleep(0.01)
        lf._running = False
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    asyncio.run(run())

    assert len(connects) >= 2, "stalled socket must reconnect"
    assert any("socket presumed dead" in m for _, m in events)


def test_is_rate_limit_block_classifier():
    """Dhan's per-IP WS connection-limit block (HTTP 429 'client id is blocked')
    must be recognised so the driver waits out the cooldown; ordinary drops and
    stalls must NOT be, so they keep the fast reconnect."""
    from app.engines.feed import LiveFeed
    blk = LiveFeed._is_rate_limit_block
    assert blk(RuntimeError(
        "InvalidStatus(Response(status_code=429, reason_phrase='Too Many "
        "Requests', body=b'Too many requests from this IP hence client id is "
        "blocked'))")) is True
    assert blk(RuntimeError("HTTP 429 Too Many Requests")) is True
    assert blk(RuntimeError("no packets for 120s — socket presumed dead")) is False
    assert blk(ConnectionResetError("connection reset by peer")) is False


def test_rate_limit_block_backs_off_long(monkeypatch):
    """A 429 'client id is blocked' at connect must trigger the long cooldown
    backoff and a distinct 'blocked by Dhan' event — not the fast 30s reconnect
    loop that keeps hammering (and can extend) the block."""
    import sys
    import types
    from app.engines.feed import LiveFeed

    class BlockedFeed:
        def __init__(self, ctx, instruments, version):
            pass

        async def connect(self):
            raise RuntimeError(
                "InvalidStatus(Response(status_code=429, reason_phrase='Too "
                "Many Requests', body=b'Too many requests from this IP hence "
                "client id is blocked'))")

        async def get_instrument_data(self):
            await asyncio.sleep(3600)

        def _is_ws_closed(self):
            return True

        async def disconnect(self):
            return None

    monkeypatch.setitem(sys.modules, "dhanhq",
                        types.SimpleNamespace(MarketFeed=BlockedFeed))

    events = []
    lf = LiveFeed(context_factory=lambda: None,
                  instruments=lambda: [(0, "13", 15)],
                  sec_to_underlying=lambda: {13: "NIFTY"},
                  on_tick=lambda *a: None,
                  on_event=lambda lvl, msg: events.append((lvl, msg)))
    lf.BLOCKED_BACKOFF_S = 0.05      # don't actually wait out 5 real minutes
    lf._running = True

    async def run():
        task = asyncio.get_running_loop().create_task(lf._driver())
        for _ in range(200):
            if any("blocked by Dhan" in m for _, m in events):
                break
            await asyncio.sleep(0.01)
        lf._running = False
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    asyncio.run(run())

    assert any("blocked by Dhan" in m for _, m in events), events
    # the ordinary "reconnecting in Ns" line must NOT be used for a block
    assert not any("reconnecting in" in m for _, m in events), events


def test_consecutive_blocks_escalate_backoff(monkeypatch):
    """A running service that keeps hitting the block must ESCALATE the wait
    (300s, 600s, 900s ...) so it eventually hands Dhan a long zero-attempt
    window. A fixed 300s retry never lets the block clear (2026-07-23: an
    all-day block a steady 5-min retry never broke)."""
    import re
    import sys
    import types
    from app.engines.feed import LiveFeed

    class BlockedFeed:
        def __init__(self, ctx, instruments, version):
            pass

        async def connect(self):
            raise RuntimeError(
                "InvalidStatus(Response(status_code=429, body=b'Too many "
                "requests from this IP hence client id is blocked'))")

        async def get_instrument_data(self):
            await asyncio.sleep(3600)

        def _is_ws_closed(self):
            return True

        async def disconnect(self):
            return None

    monkeypatch.setitem(sys.modules, "dhanhq",
                        types.SimpleNamespace(MarketFeed=BlockedFeed))

    waits = []
    events = []
    lf = LiveFeed(context_factory=lambda: None,
                  instruments=lambda: [(0, "13", 15)],
                  sec_to_underlying=lambda: {13: "NIFTY"},
                  on_tick=lambda *a: None,
                  on_event=lambda lvl, msg: events.append((lvl, msg)))
    # tiny units so the test doesn't actually sleep for minutes; escalation and
    # cap are proportional so the STRUCTURE is what's asserted, not real seconds
    lf.BLOCKED_BACKOFF_S = 0.02
    lf.BLOCKED_BACKOFF_MAX_S = 0.05

    async def run():
        task = asyncio.get_running_loop().create_task(lf._driver())
        for _ in range(400):
            # collect the "backing off Ns" numbers as they appear
            for _lvl, m in events:
                mt = re.search(r"backing off ([\d.]+)s", m)
                if mt and float(mt.group(1)) not in waits:
                    waits.append(float(mt.group(1)))
            if len(waits) >= 3:
                break
            await asyncio.sleep(0.01)
        lf._running = False
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    lf._running = True
    asyncio.run(run())

    # first block waits BLOCKED_BACKOFF_S, each consecutive one waits longer,
    # capped at BLOCKED_BACKOFF_MAX_S — never a flat repeat of the base wait
    assert waits[0] == 0.02, waits
    assert waits[1] > waits[0], waits
    assert max(waits) <= 0.05 + 1e-9, waits
    assert any("block #" in m for _, m in events), events


def test_stop_disconnects_ws_for_clean_restart():
    """On shutdown the feed must DISCONNECT the Dhan WebSocket (and wait for the
    close to flush), so a restart doesn't leave a stale session that 429-blocks
    the new process. Regression for the restart feed outage."""
    from app.engines.feed import LiveFeed

    disconnected = {"n": 0}

    class FakeFeed:
        async def disconnect(self):
            disconnected["n"] += 1

    lf = LiveFeed(context_factory=lambda: None,
                  instruments=lambda: [(0, "13", 15)],
                  sec_to_underlying=lambda: {13: "NIFTY"},
                  on_tick=lambda *a: None,
                  on_event=lambda *a: None)

    async def run():
        # a live feed loop is running on THIS loop; wire it up as the feed loop
        lf._feed = FakeFeed()
        lf._feed_loop = asyncio.get_running_loop()
        lf._running = True
        # stop() calls run_coroutine_threadsafe on _feed_loop and .result()s it;
        # run it off-thread so the loop is free to service the disconnect().
        await asyncio.get_running_loop().run_in_executor(None, lf.stop)

    asyncio.run(run())
    assert disconnected["n"] == 1
    assert lf._running is False


def test_enable_chain_resubscribes_live_socket(tmp_path, monkeypatch):
    """An MCX name enabled AFTER the WS connected must force a resubscribe
    (observed 2026-07-15: canary missing until an unrelated reconnect)."""
    from app.core import registry
    from app.data.store import SyntheticStore
    from app.data import dhan_client as dc
    from app.engines import paper as P

    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "t.db")
    registry.init_db()
    monkeypatch.setitem(dc.UNDERLYINGS, "CRUDEOIL",
                        {"security_id": 428414, "segment": "MCX_COMM",
                         "fno_segment": "MCX_COMM", "instrument": "OPTFUT"})
    hub = P.MarketHub(SyntheticStore())
    refreshes = []
    hub._started = True
    hub._livefeed = type("F", (), {"refresh": lambda self: refreshes.append(1)})()
    hub.enable_chain("CRUDEOIL")
    hub.enable_chain("CRUDEOIL")          # idempotent: no second refresh
    hub.enable_chain("NOSUCHNAME")        # not in UNDERLYINGS: no refresh
    assert refreshes == [1]
