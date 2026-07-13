"""
Live Market Feed (M2)
=====================
Turns DhanHQ's binary tick WebSocket into the ("bar", underlying, interval,
Bar) events the paper engine already consumes.

Two pieces, deliberately separated:

  CandleBuilder  — pure tick -> OHLC aggregation, no I/O. Unit-tested offline.
  LiveFeed       — owns a dhanhq MarketFeed on a DEDICATED THREAD (the SDK
                   builds its own asyncio loop and its sync wrappers call
                   run_until_complete, so it cannot share the FastAPI loop),
                   parses ticks, and bridges them to the app loop via
                   call_soon_threadsafe. Reconnects with exponential backoff
                   and logs connect/disconnect to registry.record_event.

Only index SPOT is subscribed here (Ticker mode) — enough to build the
underlying candles strategies run on. Option quotes for fills/greeks come
from the chain poller (M3), not this feed.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from app.core.contract import Bar

IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# Candle building (pure, testable)
# ---------------------------------------------------------------------------

class CandleBuilder:
    """Aggregates ticks into fixed-interval OHLC bars, labeled at the bucket
    START (a bar tagged 09:15 covers 09:15:00-09:19:59 and is emitted when the
    first tick of the next bucket arrives). Intraday only — buckets are aligned
    to the top of the hour within a single trading day."""

    def __init__(self, interval_min: int):
        self.interval_min = int(interval_min)
        self._start: Optional[datetime] = None
        self._o = self._h = self._l = self._c = 0.0
        self._v = 0.0

    def _bucket_start(self, ts: datetime) -> datetime:
        mod = ts.hour * 60 + ts.minute
        floored = (mod // self.interval_min) * self.interval_min
        return ts.replace(hour=floored // 60, minute=floored % 60,
                          second=0, microsecond=0)

    def add_tick(self, ts: datetime, price: float, volume: float = 0.0) -> Optional[Bar]:
        """Feed one tick. Returns a completed Bar when the tick opens a new
        bucket, else None."""
        b = self._bucket_start(ts)
        completed = None
        if self._start is None:
            self._open_bucket(b, price)
        elif b != self._start:
            completed = self._to_bar()
            self._open_bucket(b, price)
        self._h = max(self._h, price)
        self._l = min(self._l, price)
        self._c = price
        self._v += volume
        return completed

    def _open_bucket(self, start: datetime, price: float) -> None:
        self._start = start
        self._o = self._h = self._l = self._c = price
        self._v = 0.0

    def _to_bar(self) -> Bar:
        return Bar(self._start, self._o, self._h, self._l, self._c, self._v)

    def flush(self) -> Optional[Bar]:
        """Emit the current partial bar (e.g. at market close) and reset."""
        if self._start is None:
            return None
        bar = self._to_bar()
        self._start = None
        return bar


# ---------------------------------------------------------------------------
# Live WebSocket driver (thread-isolated MarketFeed)
# ---------------------------------------------------------------------------

class LiveFeed:
    """Runs a dhanhq MarketFeed on its own thread and pushes parsed ticks to
    `on_tick(underlying, price, ts_ist)` (invoked on `app_loop`). `instruments`
    is a zero-arg callable returning the current subscription list so a
    reconnect always picks up newly-registered underlyings.

    on_event(level, message) mirrors registry.record_event for feed lifecycle.
    """

    def __init__(self, context_factory: Callable[[], object],
                 instruments: Callable[[], list],
                 sec_to_underlying: Callable[[], dict],
                 on_tick: Callable[[str, float, datetime], None],
                 on_event: Callable[[str, str], None]):
        self._context_factory = context_factory
        self._instruments = instruments
        self._sec_to_underlying = sec_to_underlying
        self._on_tick = on_tick
        self._on_event = on_event
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._app_loop: Optional[asyncio.AbstractEventLoop] = None
        self._feed = None
        self._feed_loop: Optional[asyncio.AbstractEventLoop] = None

    def start(self, app_loop: asyncio.AbstractEventLoop) -> None:
        if self._running:
            return
        self._running = True
        self._app_loop = app_loop
        self._thread = threading.Thread(target=self._thread_main,
                                        name="dhan-marketfeed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._close_current_feed()

    def refresh(self) -> None:
        """Force a reconnect so a newly-registered underlying gets subscribed
        (the driver rebuilds the instrument list on each connect)."""
        self._close_current_feed()

    # -- thread internals ----------------------------------------------------

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._feed_loop = loop
        try:
            loop.run_until_complete(self._driver())
        finally:
            loop.close()

    async def _driver(self) -> None:
        from dhanhq import MarketFeed
        backoff = 1
        while self._running:
            instruments = self._instruments()
            if not instruments:
                await asyncio.sleep(1)
                continue
            feed = None
            try:
                feed = MarketFeed(self._context_factory(), instruments, "v2")
                self._feed = feed
                await feed.connect()
                names = sorted({self._sec_to_underlying().get(int(sid), sid)
                                for _seg, sid, *_ in instruments})
                self._event("info", f"live feed connected: {', '.join(map(str, names))}")
                backoff = 1
                while self._running and not feed._is_ws_closed():
                    pkt = await feed.get_instrument_data()
                    self._handle_packet(pkt)
            except Exception as e:  # network/auth/parse — reconnect
                self._event("warn", f"live feed error: {e!r}")
            finally:
                if feed is not None:
                    try:
                        await feed.disconnect()
                    except Exception:
                        pass
                self._feed = None
            if not self._running:
                break
            self._event("warn", f"live feed disconnected; reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def _handle_packet(self, pkt) -> None:
        # Ticker/Quote/Full packets are dicts with security_id + LTP; status
        # strings and disconnect packets are ignored here (the loop's
        # _is_ws_closed check drives reconnect).
        if not isinstance(pkt, dict):
            return
        sid = pkt.get("security_id")
        ltp = pkt.get("LTP")
        if sid is None or ltp is None:
            return
        underlying = self._sec_to_underlying().get(int(sid))
        if underlying is None:
            return
        try:
            price = float(ltp)
        except (TypeError, ValueError):
            return
        if price <= 0:
            return
        ts = datetime.now(IST).replace(tzinfo=None)  # bucket by IST arrival time
        vol = pkt.get("volume") or 0.0
        if self._app_loop is not None:
            self._app_loop.call_soon_threadsafe(self._on_tick, underlying, price, ts)

    def _close_current_feed(self) -> None:
        feed, loop = self._feed, self._feed_loop
        if feed is not None and loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(feed.disconnect(), loop)
            except Exception:
                pass

    def _event(self, level: str, msg: str) -> None:
        try:
            self._on_event(level, msg)
        except Exception:
            pass
