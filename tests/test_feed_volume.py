"""
Volume / OI plumbing + index-futures companion (step 4)
=======================================================
Offline. The live socket / real futures ids need VPS verification; here we pin
the pure logic that carries volume+OI from any volume-bearing instrument into
the Bar, and the front-month FUTIDX parser.
"""

from __future__ import annotations

from datetime import datetime

from app.core.contract import Bar
from app.data.dhan_client import parse_index_futures
from app.engines.feed import CandleBuilder, LiveFeed
from app.engines.paper import MarketHub


def _dt(h, m, s=0):
    return datetime(2026, 7, 9, h, m, s)


# --- CandleBuilder volume/OI ------------------------------------------------

def test_candle_carries_oi_latest():
    cb = CandleBuilder(5)
    cb.add_tick(_dt(9, 16), 100.0, volume=10, oi=5000)
    cb.add_tick(_dt(9, 17), 101.0, volume=5, oi=5200)   # OI updates to latest
    bar = cb.add_tick(_dt(9, 20), 102.0)
    assert bar.volume == 15 and bar.oi == 5200


def test_add_volume_folds_without_touching_price():
    cb = CandleBuilder(5)
    cb.add_tick(_dt(9, 16), 100.0, volume=10)
    cb.add_volume(7, oi=999)        # companion (future) volume/OI
    bar = cb.add_tick(_dt(9, 21), 105.0)
    assert bar.volume == 17 and bar.oi == 999
    # price untouched by the companion fold
    assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 100.0, 100.0, 100.0)


def test_add_volume_noop_before_first_price_tick():
    cb = CandleBuilder(5)
    cb.add_volume(50, oi=1)         # no bucket open yet -> ignored
    assert cb.flush() is None


# --- LiveFeed volume delta + companion routing ------------------------------

class _RecLoop:
    def call_soon_threadsafe(self, fn, *args):
        fn(*args)                   # invoke inline so callbacks record


def _feed(ticks, comps):
    f = LiveFeed(context_factory=lambda: None, instruments=lambda: [],
                 sec_to_underlying=lambda: {13: "NIFTY"},
                 on_tick=lambda *a: ticks.append(a),
                 on_event=lambda l, m: None,
                 sec_to_companion=lambda: {99: "NIFTY"},
                 on_companion=lambda *a: comps.append(a))
    f._app_loop = _RecLoop()
    return f


def test_spot_tick_volume_is_delta_of_cumulative():
    ticks, comps = [], []
    f = _feed(ticks, comps)
    f._handle_packet({"security_id": 13, "LTP": 22000, "volume": 1000})
    f._handle_packet({"security_id": 13, "LTP": 22010, "volume": 1500})
    # first sight -> delta 0; second -> 500
    assert ticks[0][:2] == ("NIFTY", 22000.0) and ticks[0][3] == 0.0
    assert ticks[1][3] == 500.0
    assert not comps


def test_companion_packet_routes_to_on_companion():
    ticks, comps = [], []
    f = _feed(ticks, comps)
    f._handle_packet({"security_id": 99, "LTP": 22015, "volume": 100, "OI": 5000})
    f._handle_packet({"security_id": 99, "LTP": 22020, "volume": 300, "OI": 5100})
    # routed to companion, not price ticks; volume is a delta, OI passes through
    assert not ticks
    assert comps[0][0] == "NIFTY" and comps[0][1] == 0.0 and comps[0][2] == 5000.0
    assert comps[1][1] == 200.0 and comps[1][2] == 5100.0


def test_cumulative_reset_contributes_zero():
    ticks, comps = [], []
    f = _feed(ticks, comps)
    f._handle_packet({"security_id": 13, "LTP": 22000, "volume": 5000})
    f._handle_packet({"security_id": 13, "LTP": 22010, "volume": 10})  # new day reset
    assert ticks[1][3] == 0.0


# --- MarketHub companion merge ----------------------------------------------

class _FakeStore:
    def option_close(self, *a, **k):
        return None


def test_hub_companion_volume_lands_in_bar():
    hub = MarketHub(_FakeStore())
    hub.TICK_FRESHNESS_S = None
    hub.register("NIFTY", 5)
    q = hub.subscribe()
    hub._on_tick("NIFTY", 100.0, _dt(9, 16), volume=10, oi=500)
    hub._on_companion("NIFTY", 5.0, 600, _dt(9, 17))    # future volume/OI
    hub._on_tick("NIFTY", 102.0, _dt(9, 21))            # rolls -> emit 09:15 bar
    _, _, _, bar = q.get_nowait()
    assert bar.volume == 15.0 and bar.oi == 600.0


def test_companion_uses_full_mode_and_exchange_correct_segment():
    from app.engines.paper import _FEED_BSE_FNO, _FEED_FULL, _FEED_NSE_FNO
    hub = MarketHub(_FakeStore())
    hub.register("NIFTY", 5)     # NSE index
    hub.register("SENSEX", 5)    # BSE index
    hub._index_fut = {
        "NIFTY": {"security_id": 111, "expiry": "2026-07-31", "segment": "NSE"},
        "SENSEX": {"security_id": 222, "expiry": "2026-07-30", "segment": "BSE"},
    }
    insts = {sid: (seg, mode) for seg, sid, mode in hub._companion_instruments()}
    # Full mode (OI + volume in one packet); NSE future -> NSE_FnO, BSE -> BSE_FnO
    assert insts["111"] == (_FEED_NSE_FNO, _FEED_FULL)
    assert insts["222"] == (_FEED_BSE_FNO, _FEED_FULL)


def test_hub_instruments_unchanged_when_companion_disabled():
    hub = MarketHub(_FakeStore())
    hub.register("NIFTY", 5)
    # _index_fut empty by default -> no companion instruments appended
    assert hub._companion_instruments() == []
    modes = {mode for _seg, _sid, mode in hub._instruments()}
    assert modes == {15}     # Ticker only


# --- front-month FUTIDX parser ----------------------------------------------

def test_parse_index_futures_front_month_and_boundary():
    rows = [
        {"SEM_INSTRUMENT_NAME": "FUTIDX", "SEM_TRADING_SYMBOL": "NIFTY-Aug2026-FUT",
         "SEM_EXPIRY_DATE": "2026-08-28", "SEM_SMST_SECURITY_ID": "112"},
        {"SEM_INSTRUMENT_NAME": "FUTIDX", "SEM_TRADING_SYMBOL": "NIFTY-Jul2026-FUT",
         "SEM_EXPIRY_DATE": "2026-07-31", "SEM_SMST_SECURITY_ID": "111"},
        {"SEM_INSTRUMENT_NAME": "FUTIDX", "SEM_TRADING_SYMBOL": "NIFTYNXT50-Jul2026-FUT",
         "SEM_EXPIRY_DATE": "2026-07-31", "SEM_SMST_SECURITY_ID": "113"},
        {"SEM_INSTRUMENT_NAME": "OPTIDX", "SEM_TRADING_SYMBOL": "NIFTY-Jul2026-24000-CE",
         "SEM_EXPIRY_DATE": "2026-07-31", "SEM_SMST_SECURITY_ID": "114"},
        {"SEM_INSTRUMENT_NAME": "FUTIDX", "SEM_TRADING_SYMBOL": "NIFTY-Jun2026-FUT",
         "SEM_EXPIRY_DATE": "2026-06-26", "SEM_SMST_SECURITY_ID": "110"},  # expired
    ]
    out = parse_index_futures(rows, ("NIFTY",), today="2026-07-18")
    assert out["NIFTY"]["security_id"] == 111       # nearest live month
    assert out["NIFTY"]["expiry"] == "2026-07-31"


def test_parse_index_futures_none_when_all_expired():
    rows = [{"SEM_INSTRUMENT_NAME": "FUTIDX", "SEM_TRADING_SYMBOL": "NIFTY-Jun2026-FUT",
             "SEM_EXPIRY_DATE": "2026-06-26", "SEM_SMST_SECURITY_ID": "110"}]
    assert parse_index_futures(rows, ("NIFTY",), today="2026-07-18") == {}
