"""
Paper Trading Engine
====================
Runs each DEPLOYED strategy in its own asyncio task, all sharing one
market-data hub (one Dhan WebSocket + one option-chain poller — chain is
rate limited to 1 unique request / 3 s, so poll centrally and fan out).

Play/Pause:
  * paused strategies still receive bars + MTM updates,
  * their ctx.enter() calls are REJECTED (returns False),
  * exits still work (stop-losses keep protecting open positions),
  * if record.square_off_on_pause is True, positions flatten on pause.

Daily P&L rows are persisted to SQLite (registry.save_paper_day) so your
dashboard shows paper performance date by date, same shape as backtests.

Market data comes from MarketHub, which drives a real dhanhq MarketFeed
(app/engines/feed.py) in production and a synthetic replay in dev
(OPTIONSLAB_SYNTHETIC=1, or automatically when the store is synthetic / no
Dhan credentials are present).
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import replace
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Optional

from app.core import registry
from app.core.contract import (Action, Bar, Context, ExpiryKind, LegSpec,
                               OptionQuote, OptionType, Position, Strategy)
from app.data.dhan_client import UNDERLYINGS
from app.engines import chain as chainmod
from app.engines import fills as F
from app.engines import margin as M
from app.engines import risk as R
from app.engines.backtest import lot_size_on
from app.engines.feed import CandleBuilder, LiveFeed

IST = timezone(timedelta(hours=5, minutes=30))

# MarketFeed subscription constants (avoid importing the SDK at module load).
_FEED_IDX = 0      # MarketFeed.IDX  — index spot segment
_FEED_NSE_FNO = 2  # MarketFeed.NSE_FNO — index/stock futures (companion volume)
_FEED_MCX = 5      # MarketFeed.MCX  — commodity segment (FUTCOM contracts)
_FEED_TICKER = 15  # MarketFeed.Ticker — LTP-only packets (enough for candles)
_FEED_QUOTE = 17   # MarketFeed.Quote  — adds day volume + OI (companion futures)
# NOTE: _FEED_NSE_FNO / _FEED_QUOTE are used only by the gated index-futures
# companion (index_futures_volume setting, default off) — VPS-verify the ints
# and the Quote packet's cumulative-volume field before trusting real volume.


def _session_date() -> str:
    """Today's trading date (IST) — the key that scopes a recoverable session."""
    return datetime.now(IST).date().isoformat()


# --- Position (de)serialization for paper-state persistence (M4) ------------

def _leg_to_dict(leg: LegSpec) -> dict:
    return {"option_type": leg.option_type.value, "action": leg.action.value,
            "strike_offset": leg.strike_offset, "expiry_kind": leg.expiry_kind.value,
            "expiry_offset": leg.expiry_offset, "lots": leg.lots, "tag": leg.tag}


def _leg_from_dict(d: dict) -> LegSpec:
    return LegSpec(OptionType(d["option_type"]), Action(d["action"]),
                   d["strike_offset"], ExpiryKind(d["expiry_kind"]),
                   d["expiry_offset"], d["lots"], d.get("tag", ""))


def _pos_to_dict(p: Position) -> dict:
    return {"id": p.id, "leg": _leg_to_dict(p.leg), "underlying": p.underlying,
            "expiry": p.expiry.isoformat() if p.expiry else None, "strike": p.strike,
            "qty": p.qty, "entry_price": p.entry_price,
            "entry_ts": p.entry_ts.isoformat(), "mtm_price": p.mtm_price,
            "fees_paid": p.fees_paid, "tag": p.tag, "stop_loss": p.stop_loss,
            "target": p.target, "margin_blocked": p.margin_blocked,
            "exit_reason": p.exit_reason, "entry_context": p.entry_context}


def _pos_from_dict(d: dict) -> Position:
    return Position(
        id=d["id"], leg=_leg_from_dict(d["leg"]), underlying=d["underlying"],
        expiry=date.fromisoformat(d["expiry"]) if d.get("expiry") else None,
        strike=d["strike"], qty=d["qty"], entry_price=d["entry_price"],
        entry_ts=datetime.fromisoformat(d["entry_ts"]), mtm_price=d["mtm_price"],
        fees_paid=d["fees_paid"], tag=d.get("tag", ""), stop_loss=d.get("stop_loss"),
        target=d.get("target"), margin_blocked=d.get("margin_blocked", 0.0),
        exit_reason=d.get("exit_reason", ""), entry_context=d.get("entry_context") or {})


class MarketHub:
    """Single source of live data shared by all strategies.

    Deployed strategies register (underlying, timeframe); the hub aggregates
    ticks into per-timeframe candles and fans out ("bar", underlying, interval,
    Bar) messages to every subscriber queue. Messages carry the interval so a
    subscriber only consumes bars at its own timeframe.

    Drivers, chosen at start():
      * LiveFeed (dhanhq MarketFeed WS) in production,
      * a synthetic replay in dev (OPTIONSLAB_SYNTHETIC=1, synthetic store, or
        missing credentials).
    Option quotes for fills still come from the store here; the live chain
    poller (M3) will supply real bid/ask.
    """

    # Which ATM-relative expiries to poll per underlying (WEEKLY 0 = current
    # week, 1 = next). Covers the common intraday + rollover cases; one chain
    # fetch yields every strike_offset for that expiry.
    CHAIN_TARGETS = (("WEEKLY", 0), ("WEEKLY", 1))
    CHAIN_MIN_INTERVAL = 3.0  # Dhan option-chain rate limit: 1 req / 3 s

    def __init__(self, store):
        self.store = store
        self.subscribers: list[asyncio.Queue] = []
        self._wanted: dict[str, set[int]] = {}                 # underlying -> intervals
        self._builders: dict[tuple[str, int], CandleBuilder] = {}
        self._started = False
        self._livefeed: Optional[LiveFeed] = None
        self._tasks: list[asyncio.Task] = []
        # Live option-chain cache: underlying -> {chain_key: OptionQuote}
        self._chain_cache: dict[str, dict[tuple, OptionQuote]] = {}
        self._chain_spot: dict[str, float] = {}   # underlying -> last chain spot
        self._chain_persisted_fp: dict[str, tuple] = {}  # dedup for the recorder
        self._expiries_cache: dict[str, tuple[date, list]] = {}
        self._chain_only: set[str] = set()   # polled for snapshots, no strategy (MCX recorder)
        self._chain_gate = asyncio.Lock()
        self._last_chain_ts = 0.0
        self._margin_client = None            # lazy dhanhq client for real margin (M5)
        self._last_heal: Optional[datetime] = None   # feed self-heal cooldown
        # Gated index-futures companion (volume/OI for volumeless index spot):
        # underlying -> {security_id, expiry, segment}. Empty unless the
        # index_futures_volume setting is on AND resolution succeeds.
        self._index_fut: dict[str, dict] = {}

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.append(q)
        return q

    # -- registration --------------------------------------------------------
    def register(self, underlying: str, interval_min: int) -> None:
        """Declare that a strategy needs `underlying` candles at `interval_min`."""
        intervals = self._wanted.setdefault(underlying, set())
        first_time_underlying = not intervals
        intervals.add(interval_min)
        self._builders.setdefault((underlying, interval_min), CandleBuilder(interval_min))
        # A brand-new underlying needs a (re)subscribe on the live socket.
        if self._started and self._livefeed and first_time_underlying:
            self._livefeed.refresh()

    def enable_chain(self, underlying: str) -> None:
        """Poll `underlying`'s option chain for snapshots even without a
        deployed strategy (used by the MCX recorder). Requires the hub to be
        started and `underlying` present in dhan_client.UNDERLYINGS."""
        first_time = underlying not in self._chain_only
        self._chain_only.add(underlying)
        # a name added AFTER the socket connected needs a resubscribe, same
        # as register() — else the WS carries the old instrument list until
        # some unrelated reconnect (observed: MCX canary missing post-boot)
        if (self._started and self._livefeed and first_time
                and underlying in UNDERLYINGS):
            self._livefeed.refresh()

    def resubscribe(self) -> None:
        """Force the live feed to reconnect and rebuild its instrument list.
        Needed when an already-tracked underlying's security id CHANGES
        underneath us (e.g. an MCX futures contract rolling over at expiry)
        — enable_chain()/register() only refresh on first-time registration,
        so a rollover otherwise leaves the socket subscribed to the old,
        now-dead contract until some unrelated reconnect happens (which can
        be hours away), silently freezing that underlying's recording."""
        if self._started and self._livefeed:
            self._livefeed.refresh()

    def self_heal_feed(self) -> dict:
        """Recovery action for a silent/down feed during market hours — the
        failure LiveFeed's dropped-socket reconnect can't catch (socket up but
        no data). An MCX commodity contract can go dead mid-session BEFORE its
        stored expiry passes, so re-resolve the MCX ids, drop the now-stale
        per-day expiry cache for those names (a new contract has new expiries),
        then force a resubscribe so the socket rebuilds around the LIVE
        contracts. Idempotent; safe to call repeatedly. Sync (network) — the
        watchdog runs it off the event loop."""
        out = {"resolved": {}, "resubscribed": False}
        tracked = set(self._wanted) | self._chain_only
        try:
            from app.data.dhan_client import MCX_DYNAMIC, resolve_mcx_ids
            if any(u in MCX_DYNAMIC for u in tracked):
                out["resolved"] = resolve_mcx_ids() or {}
                for u in MCX_DYNAMIC:
                    self._expiries_cache.pop(u, None)   # new contract → new expiries
        except Exception as e:
            registry.record_event("warn", "feed", f"self-heal resolve failed: {e!r}")
        if self._started and self._livefeed:
            self._livefeed.refresh()
            out["resubscribed"] = True
        return out

    def feed_status(self) -> dict:
        """Health surface for the dashboard's Feed pill: driver mode, socket
        state, and how fresh the last tick is."""
        if not self._started:
            return {"mode": "off", "connected": False, "last_tick": None,
                    "tick_age_sec": None}
        if self._livefeed is None:
            return {"mode": "synthetic", "connected": True, "last_tick": None,
                    "tick_age_sec": None}
        lt = self._livefeed.last_tick
        age = ((datetime.now(IST).replace(tzinfo=None) - lt).total_seconds()
               if lt else None)
        return {"mode": "live", "connected": self._livefeed.connected,
                "last_tick": lt.isoformat(sep=" ", timespec="seconds") if lt else None,
                "tick_age_sec": round(age, 1) if age is not None else None,
                # which exchange sessions this feed actually covers, so the
                # UI pill can say Off-hours (not Quiet) when only OTHER
                # exchanges are open (e.g. MCX evenings on an NSE-only feed)
                "segments": sorted(self._watch_segments())}

    def _watch_segments(self) -> set[str]:
        """Exchanges whose sessions the watchdog/pill should judge — all
        WS-subscribed underlyings, incl. chain-only MCX names (their futures
        tick 09:00-23:30, so they legitimately extend the watched window)."""
        segs = set()
        for u in set(self._wanted) | self._chain_only:
            cfg = UNDERLYINGS.get(u)
            if cfg:
                segs.add("MCX" if "MCX" in str(cfg.get("segment", "")) else "NSE")
        return segs

    async def _watchdog_loop(self) -> None:
        """Once a minute: if the feed is down/silent during market hours,
        push an ntfy alert (same channel as token refresh). See watchdog.py."""
        from app.engines.watchdog import (FeedWatchdog, feed_broken,
                                          session_open_for)
        HEAL_COOLDOWN_S = 180        # at most one self-heal attempt / 3 min
        wd = FeedWatchdog()
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(60)
            try:
                now = datetime.now(IST).replace(tzinfo=None)
                is_open = session_open_for(self._watch_segments(), now)
                status = self.feed_status()
                # step() may push over HTTP — keep it off the event loop
                kind = await loop.run_in_executor(None, wd.step, status, is_open, now)
                if kind:
                    lvl = "info" if kind == "recovered" else "warn"
                    registry.record_event(lvl, "feed", f"watchdog: feed {kind}")
                # SELF-HEAL: don't just alert — try to recover. A silent socket
                # (dead/rolled MCX contract, stale connection) won't fix itself.
                if feed_broken(status, is_open):
                    if (self._last_heal is None
                            or (now - self._last_heal).total_seconds() >= HEAL_COOLDOWN_S):
                        self._last_heal = now
                        res = await loop.run_in_executor(None, self.self_heal_feed)
                        registry.record_event(
                            "warn", "feed", f"watchdog: feed broken — self-heal {res}")
                else:
                    self._last_heal = None      # healthy again → reset cooldown
            except Exception as e:
                registry.record_event("warn", "feed", f"watchdog error: {e!r}")

    def _use_synthetic(self) -> bool:
        from app.data.store import SyntheticStore
        if os.environ.get("OPTIONSLAB_SYNTHETIC") == "1":
            return True
        if isinstance(self.store, SyntheticStore):
            return True
        try:
            from app.data import dhan_client
            dhan_client.resolve_credentials()
            return False
        except Exception:
            return True

    async def ensure_started(self) -> None:
        if self._started:
            return
        self._started = True
        if self._use_synthetic():
            registry.record_event("info", "feed", "paper feed: synthetic driver")
            self._tasks.append(asyncio.create_task(self._run_synthetic()))
        else:
            from app.data import dhan_client
            loop = asyncio.get_running_loop()
            from app.engines.watchdog import session_open_for
            if self._companion_enabled():
                self._resolve_index_futures()
            self._livefeed = LiveFeed(
                context_factory=dhan_client.get_dhan_context,
                instruments=self._instruments,
                sec_to_underlying=self._sec_to_underlying,
                on_tick=self._on_tick,
                on_event=lambda lvl, msg: registry.record_event(lvl, "feed", msg),
                watch_open=lambda: session_open_for(
                    self._watch_segments(),
                    datetime.now(IST).replace(tzinfo=None), grace_min=10),
                sec_to_companion=self._sec_to_companion,
                on_companion=self._on_companion)
            self._livefeed.start(loop)
            self._tasks.append(asyncio.create_task(self._eod_clock()))
            self._tasks.append(asyncio.create_task(self._chain_poll_loop()))
            self._tasks.append(asyncio.create_task(self._watchdog_loop()))

    async def stop(self) -> None:
        if self._livefeed:
            self._livefeed.stop()
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        self._started = False

    # -- live wiring ---------------------------------------------------------
    def _instruments(self) -> list:
        """Everything the WS should carry: strategy underlyings (index spot)
        plus chain-only MCX names (their FUTCOM future ticks 09:00-23:30 —
        both the recorder's spot reference and the feed's early-morning
        canary: proof the pipe is alive 15 min before equity opens)."""
        out = []
        for u in set(self._wanted) | self._chain_only:
            cfg = UNDERLYINGS.get(u)
            if cfg:
                seg = _FEED_MCX if cfg.get("segment") == "MCX_COMM" else _FEED_IDX
                out.append((seg, str(cfg["security_id"]), _FEED_TICKER))
        out += self._companion_instruments()   # index-futures volume (gated)
        return out

    def _sec_to_underlying(self) -> dict:
        return {UNDERLYINGS[u]["security_id"]: u
                for u in set(self._wanted) | self._chain_only
                if u in UNDERLYINGS}

    # -- index-futures companion (volume/OI for index spot; gated) -----------
    def _companion_enabled(self) -> bool:
        """On only when the setting is flipped AND we're on the live (not
        synthetic) driver — a synthetic replay has no futures feed."""
        return (registry.setting("index_futures_volume", "off") == "on"
                and not self._use_synthetic())

    def _resolve_index_futures(self) -> None:
        """Populate self._index_fut with the front-month FUTIDX id for each
        deployed INDEX underlying (best-effort; leaves it empty on failure)."""
        try:
            from app.data import dhan_client
            resolved = dhan_client.resolve_index_futures()
        except Exception as e:
            registry.record_event("warn", "feed",
                                  f"index-futures resolve failed: {e!r}")
            return
        self._index_fut = {u: resolved[u] for u in self._wanted
                           if u in resolved}

    def _companion_instruments(self) -> list:
        """Quote-mode subscription tuples for the resolved index futures — the
        volume/OI companions for index spot. NSE names -> NSE_FnO segment."""
        out = []
        for u, fut in self._index_fut.items():
            seg = _FEED_NSE_FNO   # index futures trade in the FnO segment
            out.append((seg, str(fut["security_id"]), _FEED_QUOTE))
        return out

    def _sec_to_companion(self) -> dict:
        return {int(fut["security_id"]): u for u, fut in self._index_fut.items()}

    def _emit(self, msg: tuple) -> None:
        for q in self.subscribers:
            q.put_nowait(msg)

    def _tick_ok(self, ts: datetime) -> bool:
        """Shared tick gate: regular-session, plausibly-timed ticks only. Dhan's
        feed also delivers pre-open auction prints (09:00-09:08) and stale
        snapshot ticks on (re)subscribe — observed producing a 09:00 bar,
        future-stamped bars (which tripped the 15:25 EOD branch mid-day), and
        weekend bars. All junk dies here, at the single choke point."""
        if ts.weekday() >= 5 or not (dtime(9, 15) <= ts.time() <= dtime(15, 30)):
            return False
        if self.TICK_FRESHNESS_S:          # None in tests/replays (fixed clocks)
            now = datetime.now(IST).replace(tzinfo=None)
            if abs((ts - now).total_seconds()) > self.TICK_FRESHNESS_S:
                return False
        return True

    def _on_tick(self, underlying: str, price: float, ts: datetime,
                 volume: float = 0.0, oi: float = 0.0) -> None:
        """Called on the app loop for each parsed PRICE tick; rolls candles and
        emits completed bars per registered timeframe. `volume`/`oi` are 0 for
        volumeless index spot (Ticker) — the companion future supplies them via
        _on_companion."""
        if not self._tick_ok(ts):
            return
        for interval in tuple(self._wanted.get(underlying, ())):
            builder = self._builders.get((underlying, interval))
            if builder is None:
                continue
            bar = builder.add_tick(ts, price, volume, oi)
            if bar is not None:
                self._emit(("bar", underlying, interval, bar))

    def _on_companion(self, underlying: str, volume: float, oi: float,
                      ts: datetime) -> None:
        """Fold an index future's volume/OI into every open candle for the SPOT
        underlying, without touching price (index spot already sets it). Same
        tick gate as price ticks. No bar is emitted here — the price tick that
        rolls the bucket carries the accumulated volume/OI out."""
        if not self._tick_ok(ts):
            return
        for interval in tuple(self._wanted.get(underlying, ())):
            builder = self._builders.get((underlying, interval))
            if builder is not None:
                builder.add_volume(volume, oi)

    async def _eod_clock(self) -> None:
        """At 15:31 IST, flush partial candles and emit EOD so strategies square
        off and the day is persisted even if the last tick preceded close.
        Weekdays only — an EOD on a closed day would stamp a phantom zero
        P&L row for a session that never happened."""
        while True:
            now = datetime.now(IST)
            target = now.replace(hour=15, minute=31, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            if datetime.now(IST).weekday() >= 5:
                continue
            for (u, interval), builder in self._builders.items():
                bar = builder.flush()
                if bar is not None:
                    self._emit(("bar", u, interval, bar))
            for u in list(self._wanted):
                self._emit(("eod", u, None, None))

    async def _run_synthetic(self) -> None:
        """Dev driver: replay today's synthetic bars (per registered timeframe),
        one step per second, then EOD."""
        now = datetime.now().replace(hour=9, minute=15, second=0, microsecond=0)
        end = now.replace(hour=15, minute=30)
        streams = {(u, iv): self.store.underlying_bars(u, now, end, iv)
                   for u, ivs in self._wanted.items() for iv in ivs}
        maxlen = max((len(b) for b in streams.values()), default=0)
        for i in range(maxlen):
            for (u, iv), bars in streams.items():
                if i < len(bars):
                    self._emit(("bar", u, iv, bars[i]))
            await asyncio.sleep(1.0)
        for u in list(self._wanted):
            self._emit(("eod", u, None, None))

    # -- option chain poller (M3) -------------------------------------------
    async def _chain_poll_loop(self) -> None:
        """Poll each deployed underlying's option chain (rate-limited to
        1 req / 3 s globally) and refresh the ATM-relative quote cache used by
        fills. One fetch per (underlying, expiry) covers all strike offsets."""
        from app.data import dhan_client
        loop = asyncio.get_running_loop()
        client = None
        warned: set[str] = set()
        while True:
            for u in self._chain_order():
                cfg = UNDERLYINGS.get(u)
                if not cfg:
                    continue
                # Per-underlying isolation: a failing chain (e.g. MCX) must
                # never break the others' polling. On ANY failure the client is
                # dropped and rebuilt for the next name — Dhan's error payloads
                # don't reliably say "token", and after the 24h token rotated a
                # keyword-gated rebuild kept a dead client all session, freezing
                # the quote cache at Friday's prices (2026-07-13).
                try:
                    if client is None:
                        client = await loop.run_in_executor(None, dhan_client.get_client)
                    await self._poll_one_chain(u, cfg, client, loop, self.CHAIN_TARGETS)
                    warned.discard(u)
                except Exception as e:
                    if u not in warned:
                        warned.add(u)
                        registry.record_event("warn", "feed",
                                              f"chain poll error [{u}]: {e!r}")
                    client = None          # rebuild for the next name/cycle
                    await asyncio.sleep(self.CHAIN_MIN_INTERVAL)
            await asyncio.sleep(1.0)

    def _chain_order(self) -> list:
        """Underlyings to poll each cycle, DEPLOYED FIRST. Paper fills depend
        on fresh chains for `_wanted`; the Tier-2 scanner (F3) dumps stock
        names into `_chain_only`, so deployed strategies must never queue
        behind the shortlist. Deployed names lead; chain-only names follow."""
        order = list(self._wanted)
        order += [u for u in self._chain_only if u not in self._wanted]
        return order

    async def _poll_one_chain(self, u, cfg, client, loop, targets) -> None:
        """Refresh one underlying's chain cache for `targets` expiries through
        the shared 3s gate. Single-sourced so both the deployed poll loop and
        the Tier-2 scanner use the exact same fetch/normalize/cache path (and
        the same global rate limit). Raises on hard failure; the caller owns
        client rebuild and warn-throttling."""
        expiries = await self._get_expiries(client, u, cfg, loop)
        for kind, off in targets:
            exp = chainmod.resolve_expiry(expiries, kind, off)
            if not exp:
                continue
            data = await self._fetch_chain_ratelimited(client, cfg, exp, loop)
            if not data:
                continue
            ts = datetime.now(IST).replace(tzinfo=None)
            quotes = chainmod.normalize_chain(
                data, u, kind, off, date.fromisoformat(exp), ts)
            if quotes:
                self._chain_cache.setdefault(u, {}).update(quotes)
                sp = chainmod.chain_spot(data)
                if sp:
                    self._chain_spot[u] = sp

    async def _get_expiries(self, client, underlying, cfg, loop) -> list:
        today = datetime.now(IST).date()
        cached = self._expiries_cache.get(underlying)
        if cached and cached[0] == today:
            return cached[1]
        resp = await loop.run_in_executor(
            None, lambda: client.expiry_list(under_security_id=cfg["security_id"],
                                             under_exchange_segment=cfg["segment"]))
        data = resp.get("data") if isinstance(resp, dict) else None
        expiries = data.get("data") if isinstance(data, dict) else data
        expiries = expiries or []
        self._expiries_cache[underlying] = (today, expiries)
        return expiries

    async def _fetch_chain_ratelimited(self, client, cfg, expiry, loop):
        from app.data import dhan_client
        async with self._chain_gate:
            wait = self.CHAIN_MIN_INTERVAL - (time.monotonic() - self._last_chain_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await loop.run_in_executor(
                    None, dhan_client.fetch_option_chain,
                    client, cfg["security_id"], cfg["segment"], expiry)
            finally:
                self._last_chain_ts = time.monotonic()

    def persist_chain_snapshots(self, store, underlyings=None) -> int:
        """Write the current cached chain quotes into option_bars (snapshot:
        o=h=l=c=ltp), keyed ATM-relative. Used by the MCX recorder to build the
        expired-options history Dhan doesn't provide for commodities."""
        from app.data.dhan_client import upsert_option_rows
        ts = datetime.now(IST).replace(tzinfo=None)
        rows = []
        for u, cache in self._chain_cache.items():
            if underlyings and u not in underlyings:
                continue
            for (kind, off, soff, otype), q in cache.items():
                rows.append((u, ts, kind, off, soff, otype, q.strike, q.expiry,
                             q.ltp, q.ltp, q.ltp, q.ltp, q.volume or 0, q.oi or 0, q.iv))
        return upsert_option_rows(store, rows)

    def _chain_fingerprint(self, u: str):
        """Cheap content hash of an underlying's cached chain. On a holiday /
        closed session Dhan keeps serving the last session's FROZEN chain — the
        poller restamps it with fresh timestamps, so timestamps can't detect
        staleness. Actual price/OI change can: frozen chain -> identical
        fingerprint -> the recorder skips it (no junk rows, no fake learning
        day), with no holiday calendar needed."""
        cache = self._chain_cache.get(u)
        if not cache:
            return None
        return (self._chain_spot.get(u),
                round(sum(q.ltp or 0 for q in cache.values()), 2),
                round(sum(q.oi or 0 for q in cache.values()), 1),
                len(cache))

    def chain_changed(self, underlyings) -> list[str]:
        """Subset of `underlyings` whose chain content moved since the last
        persisted snapshot (i.e. the market is genuinely trading)."""
        out = []
        for u in underlyings:
            fp = self._chain_fingerprint(u)
            if fp is not None and fp != self._chain_persisted_fp.get(u):
                out.append(u)
        return out

    def mark_chain_persisted(self, underlyings) -> None:
        for u in underlyings:
            fp = self._chain_fingerprint(u)
            if fp is not None:
                self._chain_persisted_fp[u] = fp

    def persist_chain_full(self, store, underlyings=None,
                           max_age_s: float = 600.0) -> int:
        """Write full-fidelity snapshot rows (bid/ask/IV/OI/volume/greeks +
        spot) into chain_snapshots — the edge-research dataset option_bars'
        OHLC shape can't hold. Skips quotes older than `max_age_s` so an
        after-hours recorder tick doesn't restamp stale prices as fresh."""
        if not hasattr(store, "upsert_chain_rows"):
            return 0
        now = datetime.now(IST).replace(tzinfo=None)
        rows = []
        for u, cache in self._chain_cache.items():
            if underlyings and u not in underlyings:
                continue
            spot = self._chain_spot.get(u)
            for (kind, off, soff, otype), q in cache.items():
                if (now - q.ts).total_seconds() > max_age_s:
                    continue
                rows.append((u, q.ts, q.expiry, kind, off, q.strike, soff,
                             otype, spot, q.ltp, q.bid, q.ask, q.iv, q.oi,
                             q.volume, q.delta, q.theta, q.vega, q.gamma))
        return store.upsert_chain_rows(rows)

    def market_client(self):
        """Lazily-built dhanhq client for real margin queries; None in
        synthetic/dev or when credentials are missing (real_margin then falls
        back to the estimate). Separate from the chain poller's client so the
        requests.Session isn't shared across the loop thread and executor."""
        if self._use_synthetic():
            return None
        if self._margin_client is None:
            try:
                from app.data import dhan_client
                self._margin_client = dhan_client.get_client()
            except Exception:
                self._margin_client = None
        return self._margin_client

    QUOTE_MAX_AGE_S = 600.0   # a cached quote older than this is FROZEN, not live
    TICK_FRESHNESS_S: Optional[float] = 600.0   # tick wall-clock sanity (None = off)

    def quote(self, underlying: str, ts: datetime, leg: LegSpec) -> Optional[OptionQuote]:
        """Real bid/ask/greeks from the live chain cache when available, else
        the store (backfill / synthetic) so backtests and dev keep working.
        NOTE: keyed ATM-relative — correct for NEW entries only. Marking or
        exiting an OPEN position must go through quote_position().

        STALENESS GUARD: if a live cache exists but its quote is old, return
        None — refusing to price beats pricing at a frozen chain (a dead
        poller once served Friday's closes as 'live' and a paper entry filled
        ~25% off the real market). No store fallback in that case: on a live
        trading day the store is even staler."""
        cache = self._chain_cache.get(underlying)
        if cache:
            key = (leg.expiry_kind.value, leg.expiry_offset,
                   leg.strike_offset, leg.option_type.value)
            q = cache.get(key)
            if q is not None:
                if (ts - q.ts).total_seconds() > self.QUOTE_MAX_AGE_S:
                    return None
                return replace(q, ts=ts)
        return self.store.option_close(underlying, ts, leg)

    def quote_position(self, underlying: str, ts: datetime, pos) -> Optional[OptionQuote]:
        """Quote the ACTUAL contract an open position holds (fixed strike +
        expiry + type). The leg-key cache re-anchors to the current ATM every
        poll, so marking a position by its ATM-relative leg silently re-prices
        it to a DIFFERENT contract as spot moves — and a live exit would even
        route the order to the wrong security_id. Scans the cached chain
        (full chain per expiry) for the position's contract; falls back to the
        store's by-strike series (backfill/dev)."""
        otype = pos.leg.option_type
        cache = self._chain_cache.get(underlying)
        if cache:
            for q in cache.values():
                if q.option_type != otype or q.strike != pos.strike:
                    continue
                if pos.expiry is not None and q.expiry is not None \
                        and q.expiry != pos.expiry:
                    continue
                return replace(q, ts=ts)
        if hasattr(self.store, "option_series_by_strike"):
            rows = self.store.option_series_by_strike(
                underlying, pos.strike, otype.value,
                pos.leg.expiry_kind.value, pos.leg.expiry_offset, ts)
            if rows:
                return OptionQuote(ts, underlying, pos.expiry, pos.strike,
                                   otype, ltp=rows[-1][1])
        return self.store.option_close(underlying, ts, pos.leg)


class PaperContext(Context):
    def __init__(self, record: registry.StrategyRecord, underlying: str,
                 hub: MarketHub, interval: int = 5):
        self.rec = record
        self.underlying = underlying
        self.hub = hub
        self.interval = int(interval)  # this strategy's timeframe (minutes)
        self.fee_cfg, self.slip_cfg = F.FeeConfig(), F.SlippageConfig()
        self.paused = record.state != registry.State.RUNNING
        self._bars: list[Bar] = []          # live session bars only
        self._warmup: list[Bar] = []        # pre-session bars for indicator lookback
        self._open: list[Position] = []
        self.closed_today: list[Position] = []
        self._margin_used = 0.0
        self._realized_today = 0.0
        self._fees_today = 0.0
        self._day: Optional[date] = None   # trading date the counters belong to

    # -- engine wiring -------------------------------------------------------
    def _roll_day(self, ts: datetime) -> None:
        """Reset day counters when the trading date changes. Resetting here (not
        in persist_day) keeps persist_day IDEMPOTENT — it is called on every bar
        after 15:25 and on stop, and must never zero out the day it just saved."""
        d = ts.date()
        if self._day is None:
            self._day = d
        elif d != self._day:
            self._day = d
            self._realized_today = 0.0
            self._fees_today = 0.0
            self.closed_today = []

    def push_bar(self, bar: Bar) -> None:
        self._roll_day(bar.ts)
        self._bars.append(bar)
        for p in self._open:
            q = self.hub.quote_position(self.underlying, bar.ts, p)  # actual contract
            if q:
                p.mtm_price = q.ltp
        self._enforce_levels()

    def refresh_mtm(self, ts: datetime) -> None:
        """Mark open positions from the live chain cache between bar closes.
        The chain poller refreshes every ~3s, but push_bar (and with it the
        displayed P&L) only fires once per the strategy's own timeframe —
        5+ minutes for most strategies — so without this, P&L looked frozen
        between bars even though fresh quotes were already in memory."""
        for p in self._open:
            q = self.hub.quote_position(self.underlying, ts, p)
            if q:
                p.mtm_price = q.ltp

    def _enforce_levels(self) -> None:
        for p in list(self.positions):
            hit = F.level_hit(p.qty, p.mtm_price, p.stop_loss, p.target)
            if hit:
                self.log(f"{hit} hit on {p.tag or p.id} @ {p.mtm_price}")
                self._close(p, reason=hit)

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        if paused and self.rec.square_off_on_pause:
            for p in list(self.positions):
                self._close(p, reason='pause')

    # -- Context ---------------------------------------------------------------
    @property
    def now(self) -> datetime:
        return self._bars[-1].ts if self._bars else datetime.now()

    @property
    def lot_size(self) -> int:
        return lot_size_on(self.underlying, self.now.date())

    @property
    def spot(self) -> float:
        return self._bars[-1].close if self._bars else 0.0

    def option(self, leg: LegSpec) -> Optional[OptionQuote]:
        return self.hub.quote(self.underlying, self.now, leg)

    def history(self, n: int, interval: Optional[int] = None) -> list[Bar]:
        # A different timeframe than our own -> resample from the store (kept
        # current by upsert_live_bar). Higher TFs work; the strategy's own TF
        # returns the live in-memory bars (fresher than the store).
        if (interval is not None and interval != self.interval
                and hasattr(self.hub.store, "history_bars")):
            return self.hub.store.history_bars(self.underlying, self.now, interval, n)
        # warmup bars (loaded from the store at deploy) prepend the live
        # session so an indicator strategy isn't blind after a mid-session
        # (re)start — see warmup(). They never affect now/spot/day accounting
        # (those read _bars only), so persist_day's no-bar guard is preserved.
        if self._warmup:
            return (self._warmup + self._bars)[-n:]
        return self._bars[-n:]

    def signal(self, name: str):
        """Live FNO-scanner read for this underlying (F6)."""
        from app.engines import signals
        return signals.get_signal(self.underlying, name)

    def chain(self) -> Optional[dict]:
        """Live chain summary from the hub's shared cache for this underlying."""
        cache = self.hub._chain_cache.get(self.underlying)
        if not cache:
            return None
        from app.engines.scanner import chain_summary
        return chain_summary(cache)

    def iv_rank(self, lookback_days: int = 30) -> Optional[float]:
        if not self._bars or not hasattr(self.hub.store, "atm_iv_chain_series"):
            return None
        series = self.hub.store.atm_iv_chain_series(
            self.underlying, self.now, lookback_days)
        if len(series) < 5:
            return None
        from app.engines.indicators import percentile_rank
        return percentile_rank(series[-1], series)

    @property
    def positions(self) -> list[Position]:
        return [p for p in self._open if p.is_open]

    @property
    def allocated_capital(self) -> float:
        return self.rec.allocated_capital

    @property
    def available_capital(self) -> float:
        return self.rec.allocated_capital - self._margin_used

    @property
    def day_pnl(self) -> float:
        return self._realized_today + sum(p.unrealized_pnl for p in self.positions)

    def enter(self, legs: list[LegSpec], tag: str = "",
              sl_pct=None, target_pct=None) -> bool:
        if self.paused:
            self.log(f"enter blocked: strategy paused ({tag})")
            return False
        quotes = [(leg, self.option(leg)) for leg in legs]
        if any(q is None for _, q in quotes):
            return False
        seg = UNDERLYINGS.get(self.underlying, {}).get("fno_segment", "NSE_FNO")
        est = M.real_margin(
            [{"security_id": q.security_id, "action": leg.action,
              "qty_units": leg.lots * self.lot_size, "price": q.ltp}
             for leg, q in quotes],
            self.spot, self.lot_size, underlying=self.underlying,
            client=self.hub.market_client(), segment=seg)
        if est > self.available_capital:
            self.log(f"enter blocked: margin {est:,.0f} > available {self.available_capital:,.0f}")
            return False
        margin_share = est / max(1, len(quotes))
        from app.engines.attribution import capture_entry_context
        entry_ctx = capture_entry_context(self)   # data state for attribution
        for leg, q in quotes:
            units = leg.lots * self.lot_size
            res = F.fill_live(q, leg.action, units, self.fee_cfg, self.slip_cfg)
            qty = units if leg.action == Action.BUY else -units
            sl, tgt = F.levels_for(res.price, qty, sl_pct, target_pct)
            pos = Position(
                id=str(uuid.uuid4())[:8], leg=leg, underlying=self.underlying,
                expiry=q.expiry, strike=q.strike, qty=qty,
                entry_price=res.price, entry_ts=self.now, mtm_price=res.price,
                fees_paid=res.fees, tag=tag or leg.tag,
                stop_loss=sl, target=tgt, margin_blocked=round(margin_share, 2),
                entry_context=dict(entry_ctx))
            self._open.append(pos)
            self._fees_today += res.fees
            self._blotter(pos, leg.action.value, res.price, res.fees,
                          margin_share, "entry")
        self._margin_used += est
        self.persist_state()  # M4: durable on every fill
        return True

    def set_levels(self, position_id: str, stop_loss=None, target=None) -> bool:
        for p in self._open:
            if p.id == position_id and p.is_open:
                if stop_loss is not None:
                    p.stop_loss = stop_loss
                if target is not None:
                    p.target = target
                return True
        return False

    def _blotter(self, p: Position, side: str, price: float, fees: float,
                 margin: float, reason: str) -> None:
        registry.record_event("info", "fill",
                              f"{side} {abs(p.qty)} {p.underlying} {p.strike:g} "
                              f"{'CE' if p.leg.option_type.value=='CALL' else 'PE'} "
                              f"@ {price} ({reason})", self.rec.id)
        row = {
            "ts": self.now.isoformat(sep=" ", timespec="seconds"),
            "contract": (f"{p.underlying} {p.expiry:%d%b%y} "
                         f"{p.strike:g} {'CE' if p.leg.option_type.value == 'CALL' else 'PE'}").upper(),
            "side": side, "qty": abs(p.qty), "price": price,
            "fees": round(fees, 2), "margin": round(margin, 2),
            "reason": reason, "tag": p.tag,
        }
        if reason == "entry" and p.entry_context:
            row["entry_context"] = p.entry_context   # signal attribution
        registry.record_trade(self.rec.id, "PAPER", row)

    def exit(self, position_id: str, reason: str = "signal") -> bool:
        for p in self._open:
            if p.id == position_id and p.is_open:
                self._close(p, reason=reason)
                return True
        return False

    def exit_all(self, reason: str = "signal") -> None:
        for p in list(self.positions):
            self._close(p, reason=reason)

    def _close(self, p: Position, reason: str = "signal") -> None:
        q = self.hub.quote_position(self.underlying, self.now, p)  # actual contract
        action = Action.SELL if p.qty > 0 else Action.BUY
        fees = 0.0
        if q:
            res = F.fill_live(q, action, abs(p.qty), self.fee_cfg, self.slip_cfg)
            p.exit_price, fees = res.price, res.fees
            p.fees_paid += fees
            self._fees_today += fees
        else:
            p.exit_price = p.mtm_price
        p.exit_ts = self.now
        p.exit_reason = reason
        self._realized_today += p.realized_pnl
        self.closed_today.append(p)
        self._open.remove(p)
        # release the closed leg's margin share (all released when flat —
        # a stuck _margin_used survived restarts via the state snapshot)
        self._margin_used = max(0.0, self._margin_used - (p.margin_blocked or 0.0))
        if not self._open:
            self._margin_used = 0.0
        self._blotter(p, action.value, p.exit_price, fees, 0.0, reason)
        self.persist_state()  # M4: durable on every close

    def log(self, msg: str) -> None:
        print(f"[{self.rec.id} {self.now:%H:%M}] {msg}")
        lvl = "warn" if "blocked" in msg or "hit" in msg else "info"
        kind = ("stop_loss" if "stop_loss" in msg else
                "block" if "blocked" in msg else "strategy")
        registry.record_event(lvl, kind, msg, self.rec.id)

    def warmup(self, store, n: int) -> int:
        """Seed history() with the last `n` bars recorded BEFORE now from the
        store, so an indicator strategy has lookback immediately on (re)start
        instead of trading blind until enough live bars accrue (e.g. a 20-EMA
        strategy restarted at 09:15 would otherwise be blind until ~10:55).

        Bars only — open positions are recovered separately by restore_state().
        Loaded into a dedicated list that history() prepends but now/spot/day
        counters ignore, so no phantom-day risk. No-op if history is already
        populated or n<=0. Never raises — warmup is best-effort."""
        if n <= 0 or self._warmup or self._bars:
            return 0
        try:
            now = datetime.now(IST).replace(tzinfo=None)
            per_day = max(1, 375 // max(1, self.interval))
            lookback_days = max(5, (n // per_day + 2) * 2)
            prior = store.underlying_bars(
                self.underlying, now - timedelta(days=lookback_days), now,
                self.interval)
            prior = [b for b in prior if b.ts < now]
            if prior:
                self._warmup = prior[-n:]
            return len(self._warmup)
        except Exception:
            return 0

    # -- persistence (M4) ------------------------------------------------------
    def persist_state(self) -> None:
        """Snapshot open positions + margin/P&L to SQLite so a restart mid-
        session can recover this strategy exactly where it left off."""
        registry.save_paper_state(self.rec.id, {
            # Label the snapshot with the trading day the counters ACTUALLY
            # belong to (self._day), not the wall clock. Over a weekend/holiday
            # no bar arrives, so _roll_day never fires and _realized_today still
            # holds the last session's P&L — stamping it with today's wall-clock
            # date would make restore_state treat it as "same session" and carry
            # yesterday's P&L into today (observed after a Saturday restart:
            # dashboard showed Friday's -₹ under today's date). Fall back to the
            # wall clock only before the first bar, when there are no counters.
            "date": (self._day.isoformat() if self._day else _session_date()),
            "margin_used": round(self._margin_used, 2),
            "realized_today": round(self._realized_today, 2),
            "fees_today": round(self._fees_today, 2),
            "positions": [_pos_to_dict(p) for p in self._open if p.is_open],
        })

    def restore_state(self) -> int:
        """Reload a same-session snapshot on startup. Returns the number of
        open positions recovered (0 if none / stale / different day)."""
        snap = registry.load_paper_state(self.rec.id)
        if not snap:
            return 0
        if snap.get("date") != _session_date():
            registry.clear_paper_state(self.rec.id)  # yesterday's — intraday only
            return 0
        self._open = [_pos_from_dict(d) for d in snap.get("positions", [])]
        self._margin_used = snap.get("margin_used", 0.0)
        if not self._open:
            self._margin_used = 0.0   # flat book: never restore stuck margin
        self._realized_today = snap.get("realized_today", 0.0)
        self._fees_today = snap.get("fees_today", 0.0)
        self._day = date.fromisoformat(snap["date"])   # counters belong to this day
        return len(self._open)

    # -- day close -------------------------------------------------------------
    def persist_day(self) -> None:
        """Write the CURRENT day totals. Idempotent: safe to call repeatedly
        (every bar after 15:25, and on stop) — it always writes the full day,
        never a delta. Counters roll over in _roll_day when the date changes.

        NO-BAR GUARD: never write a day the market didn't produce bars for
        (with no bars, `now` falls back to wall-clock — an EOD tick or stop()
        on a weekend/holiday would stamp a phantom ₹0 row for a session that
        never traded)."""
        if not self._bars:
            return
        unreal = sum(p.unrealized_pnl for p in self.positions)
        day = self.now.date().isoformat()
        # equity chains from the previous session's close (falls back to
        # allocated capital on day 1) — else the curve forgets every past day
        base = registry.prev_equity(self.rec.id, day, "PAPER")
        if base is None:
            base = self.rec.allocated_capital
        registry.save_paper_day(
            self.rec.id, day,
            round(self._realized_today, 2), round(unreal, 2),
            round(self._fees_today, 2),
            round(base + self._realized_today + unreal, 2))
        self.persist_state()


class PaperRunner:
    """Owns all live strategy tasks. The API layer calls play/pause/stop."""

    HEARTBEAT_SEC = 60
    MTM_REFRESH_SEC = 5

    def __init__(self, hub: MarketHub):
        self.hub = hub
        self.contexts: dict[str, PaperContext] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self._hb_task: Optional[asyncio.Task] = None
        self._mtm_task: Optional[asyncio.Task] = None

    async def deploy(self, record: registry.StrategyRecord, strategy: Strategy,
                     restore: bool = False) -> None:
        meta = strategy.meta()
        interval = int(meta.timeframe)
        ctx = PaperContext(record, meta.underlying, self.hub, interval)
        self.contexts[record.id] = ctx
        if restore:
            n = ctx.restore_state()  # before the loop starts — no race on _open
            if n:
                registry.record_event("info", "engine",
                                      f"recovered {n} open positions", record.id)
        # warmup: seed indicator lookback from the store (best-effort). A
        # strategy declares its need via meta.params["warmup_bars"].
        w = int((meta.params or {}).get("warmup_bars", 0) or 0)
        if w:
            seeded = ctx.warmup(self.hub.store, w)
            if seeded:
                registry.record_event("info", "engine",
                                      f"warmup: seeded {seeded} history bars", record.id)
        self.hub.register(meta.underlying, interval)
        await self.hub.ensure_started()
        self._ensure_heartbeat()
        self._ensure_mtm_refresh()
        self.tasks[record.id] = asyncio.create_task(self._loop(record.id, strategy, ctx))

    async def restore_all(self, instantiate) -> int:
        """On startup, re-deploy every strategy left RUNNING/DEPLOYED_PAUSED and
        recover its open positions. `instantiate(record) -> Strategy`.

        GUARD: if the market store is synthetic (real DuckDB unavailable — e.g.
        locked by a running backfill, or dev mode), do NOT auto-restore. A
        strategy deployed for real paper trading must never silently resume on
        FAKE prices and write synthetic fills into the real PAPER ledger."""
        if self.hub._use_synthetic():
            if any(rec.state in (registry.State.RUNNING, registry.State.DEPLOYED_PAUSED)
                   for rec in registry.list_all()):
                registry.record_event("warn", "engine",
                    "paper restore skipped: market store is synthetic (real data "
                    "unavailable). Deployed strategies will resume on next restart "
                    "once real data is available.")
            return 0
        recovered = 0
        for rec in registry.list_all():
            if rec.state in (registry.State.RUNNING, registry.State.DEPLOYED_PAUSED):
                try:
                    await self.deploy(rec, instantiate(rec), restore=True)
                    recovered += 1
                except Exception as e:
                    registry.record_event("error", "engine",
                                          f"restore failed: {e!r}", rec.id)
        if recovered:
            registry.record_event("info", "engine",
                                  f"restored {recovered} paper strategies")
        return recovered

    def _ensure_heartbeat(self) -> None:
        if self._hb_task is None or self._hb_task.done():
            self._hb_task = asyncio.create_task(self._heartbeat())

    def _ensure_mtm_refresh(self) -> None:
        if self._mtm_task is None or self._mtm_task.done():
            self._mtm_task = asyncio.create_task(self._mtm_refresh_loop())

    async def _mtm_refresh_loop(self) -> None:
        """Keep displayed P&L moving with live prices between bar closes —
        see PaperContext.refresh_mtm for why push_bar alone isn't enough."""
        while True:
            await asyncio.sleep(self.MTM_REFRESH_SEC)
            now = datetime.now(IST).replace(tzinfo=None)
            for ctx in list(self.contexts.values()):
                try:
                    ctx.refresh_mtm(now)
                except Exception as e:
                    registry.record_event("warn", "engine", f"mtm refresh failed: {e!r}")

    async def _heartbeat(self) -> None:
        """Persist every live context on an interval so an ungraceful crash
        (between fills) still leaves a recent recoverable snapshot."""
        while True:
            await asyncio.sleep(self.HEARTBEAT_SEC)
            for ctx in list(self.contexts.values()):
                try:
                    ctx.persist_state()
                except Exception as e:
                    registry.record_event("warn", "engine", f"heartbeat persist failed: {e!r}")

    async def _loop(self, sid: str, strategy: Strategy, ctx: PaperContext) -> None:
        q = self.hub.subscribe()
        strategy.on_start(ctx)
        while True:
            kind, underlying, interval, bar = await q.get()
            if underlying != ctx.underlying:
                continue
            if interval is not None and interval != ctx.interval:
                continue  # a bar for a different timeframe on this underlying
            if kind == "eod" or (bar and bar.ts.time() >= dtime(15, 25)):
                if bar:
                    ctx.push_bar(bar)
                # square off expiring positions
                for p in list(ctx.positions):
                    if p.expiry <= ctx.now.date():
                        ctx._close(p, reason='expiry')
                strategy.on_day_end(ctx)
                ctx.persist_day()
                if kind == "eod":
                    continue
            ctx.push_bar(bar)
            try:
                strategy.on_bar(ctx, bar)
            except Exception as e:
                ctx.log(f"STRATEGY ERROR (auto-paused): {e!r}")
                registry.record_event("error", "engine",
                                      f"Strategy crashed and was auto-paused: {e!r}", sid)
                ctx.set_paused(True)
                registry.transition(sid, registry.State.DEPLOYED_PAUSED)
            self.enforce_risk()  # M7: risk guardrails every bar

    def _auto_pause(self, sid: str, level: str, reason: str) -> bool:
        """Pause one strategy for a risk breach (idempotent). Returns True if it
        actually transitioned (so callers only log/event on real pauses)."""
        ctx = self.contexts.get(sid)
        if not ctx or ctx.paused:
            return False
        ctx.set_paused(True)
        registry.record_event(level, "risk", reason, sid)
        try:
            registry.transition(sid, registry.State.DEPLOYED_PAUSED)
        except Exception:
            pass
        return True

    def enforce_risk(self) -> None:
        """M7: pause strategies breaching their daily loss cap, and pause ALL
        strategies if the portfolio max daily loss is breached."""
        ev = R.evaluate(self.contexts)
        for b in ev["strategy_breaches"]:
            self._auto_pause(b["sid"], "error",
                            f"daily loss cap ₹{b['cap']:,.0f} hit "
                            f"(day P&L ₹{b['day_pnl']:,.0f}); strategy paused")
        if ev["portfolio_breach"]:
            paused_any = [sid for sid in list(self.contexts)
                          if self._auto_pause(sid, "error",
                              "portfolio max daily loss breached; strategy paused")]
            if paused_any:
                registry.record_event("error", "risk",
                    f"Portfolio max daily loss ₹{ev['max_loss']:,.0f} breached "
                    f"(day P&L ₹{ev['portfolio_day_pnl']:,.0f}); all strategies paused")

    def risk_snapshot(self) -> dict:
        return R.snapshot(self.contexts)

    def play(self, sid: str) -> None:
        if sid in self.contexts:
            self.contexts[sid].set_paused(False)

    def pause(self, sid: str) -> None:
        if sid in self.contexts:
            self.contexts[sid].set_paused(True)

    async def stop(self, sid: str) -> None:
        if sid in self.contexts:
            self.contexts[sid].exit_all(reason="squareoff")
            self.contexts[sid].persist_day()
        if sid in self.tasks:
            self.tasks[sid].cancel()
            del self.tasks[sid]
        self.contexts.pop(sid, None)
        registry.clear_paper_state(sid)  # stopped -> nothing to recover
