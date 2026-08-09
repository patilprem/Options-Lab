"""
Live Fill Reconciliation (M8) — the LIVE ledger vs what the broker actually did
==============================================================================
LiveContext books a position the instant an order is ACCEPTED, at the LIMIT
price it asked for. That is intent, not fact. A limit order can rest unfilled,
fill partially, fill at a better price, or be rejected after acceptance — and
until this module existed none of that reached the ledger, so the LIVE P&L was
a record of what we meant to do.

Three pieces, deliberately separated (same split as engines/feed.py):

  parse_order_update / reconcile
        PURE. Broker payload -> normalized OrderUpdate; (what we assumed, what
        happened) -> Correction. No I/O, unit-tested offline against sample
        payloads — the dhanhq SDK is only imported inside the driver.
  FillReconciler
        Holds the in-flight intents, applies updates to them, and sweeps for
        orders that never reached a terminal state. Pure state machine; the
        ledger mutation is a callback the caller supplies.
  LiveOrderFeed
        Owns a dhanhq OrderUpdate socket on a DEDICATED THREAD (the SDK builds
        its own asyncio loop) and bridges updates to the app loop via
        call_soon_threadsafe. Reconnects with exponential backoff — the SDK's
        connect_order_update() has no retry of its own and returns the moment
        the socket drops.

BOOK-THEN-CORRECT, not wait-for-fill. If the update socket never connects, the
ledger behaves exactly as it did before this module: as-placed. Waiting for a
confirmation that may never arrive would lose positions the broker really did
open, which is the worse failure. So an unreconciled ledger is the degraded
mode, and it is VISIBLE — status() feeds /live/status and the sweep raises an
error event for every order still unconfirmed after STALE_S.

TERMINAL-ONLY. PART_TRADED arrives while the order is still working and its
filled quantity keeps moving; the terminal update carries the final numbers.
Correcting on both would double-apply. So corrections happen once, on the
terminal update, and a partial that never terminates is caught by the sweep.

VPS-PENDING: the shape of Dhan's order_alert payload is verified only against
the installed SDK's own handler (Data.orderNo / Data.status). The field names
for filled quantity and average price are NOT in the SDK, so the parser accepts
every spelling Dhan's REST order object uses. Run a real order through it on
the VPS before trusting the corrections — log_unparsed() prints any payload the
parser could not read, which is the thing to watch for.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

IST = timezone(timedelta(hours=5, minutes=30))

# Dhan's order lifecycle. Only the terminal ones move the ledger.
TERMINAL = frozenset({"TRADED", "REJECTED", "CANCELLED", "EXPIRED"})

# Every spelling seen across Dhan's REST order object, the order-update feed
# and the SDK's own examples, mapped to the lifecycle above. Compared after
# upper-casing and folding spaces/hyphens to underscores.
_STATUS = {
    "TRADED": "TRADED", "COMPLETE": "TRADED", "COMPLETED": "TRADED",
    "FILLED": "TRADED", "EXECUTED": "TRADED",
    "PART_TRADED": "PART_TRADED", "PARTIALLY_TRADED": "PART_TRADED",
    "PARTIAL": "PART_TRADED", "PARTIALLY_FILLED": "PART_TRADED",
    "REJECTED": "REJECTED", "REJECT": "REJECTED",
    "CANCELLED": "CANCELLED", "CANCELED": "CANCELLED",
    "EXPIRED": "EXPIRED",
    "PENDING": "PENDING", "OPEN": "PENDING", "TRANSIT": "PENDING",
    "MODIFIED": "PENDING", "TRIGGER_PENDING": "PENDING", "VALIDATION": "PENDING",
}

_ID_KEYS = ("orderNo", "orderId", "order_id", "OrderNo", "OrderId", "id")
_STATUS_KEYS = ("status", "orderStatus", "order_status", "Status", "OrderStatus")
_QTY_KEYS = ("filledQty", "filled_qty", "tradedQuantity", "traded_quantity",
             "filledQuantity", "cumulativeQuantity", "FilledQty")
_PRICE_KEYS = ("averageTradedPrice", "average_traded_price", "avgPrice",
               "averagePrice", "avg_price", "tradedPrice", "AvgTradedPrice")
_REMARK_KEYS = ("omsErrorDescription", "oms_error_description", "remarks",
                "reason", "errorMessage", "omsErrorCode", "text")


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _first(d: dict, keys) -> Optional[object]:
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def normalize_status(raw) -> str:
    """Broker status string -> one of the lifecycle values above. An unknown
    status maps to "UNKNOWN" and is deliberately NOT terminal: acting on a
    status we don't understand is worse than leaving the order in flight for
    the sweep to flag."""
    if raw is None:
        return "UNKNOWN"
    s = str(raw).strip().upper().replace(" ", "_").replace("-", "_")
    return _STATUS.get(s, "UNKNOWN")


@dataclass(frozen=True)
class OrderUpdate:
    """One normalized broker order update."""
    order_id: str
    status: str
    filled_qty: float
    avg_price: Optional[float]
    remarks: str
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL


def parse_order_update(msg) -> Optional[OrderUpdate]:
    """Dhan order_alert payload -> OrderUpdate, or None if it isn't one.

    The SDK's own handler keys on msg['Type'] == 'order_alert' with the body
    under msg['Data'], so that is the shape accepted here; a bare body (what a
    test fixture or a future SDK version might hand over) is accepted too.
    Anything without an order id is not an order update.
    """
    if not isinstance(msg, dict):
        return None
    if msg.get("Type") not in (None, "order_alert"):
        return None
    data = msg.get("Data")
    if not isinstance(data, dict):
        data = msg
    oid = _first(data, _ID_KEYS)
    if oid in (None, ""):
        return None
    remarks = _first(data, _REMARK_KEYS)
    return OrderUpdate(
        order_id=str(oid),
        status=normalize_status(_first(data, _STATUS_KEYS)),
        filled_qty=_f(_first(data, _QTY_KEYS)) or 0.0,
        avg_price=_f(_first(data, _PRICE_KEYS)),
        remarks=str(remarks) if remarks is not None else "",
        raw=data)


@dataclass
class Intent:
    """What the ledger ASSUMED when an order was placed. One per order."""
    order_id: str
    sid: str                 # strategy id
    kind: str                # "entry" | "exit"
    position_id: str
    price: float             # the price we booked
    qty: float               # units we booked (absolute)
    fees: float              # the fees we charged for it
    placed_ts: datetime
    label: str = ""          # human-readable, for the log line


@dataclass
class Correction:
    """What the ledger must do about it. `price`/`qty` are the TRUTH, not
    deltas — the caller assigns them."""
    action: str              # "confirm" | "amend" | "void"
    price: Optional[float] = None
    qty: Optional[float] = None
    reason: str = ""

    @property
    def changes_ledger(self) -> bool:
        return self.action != "confirm"


# Ignore price differences below this (paise-level rounding between our
# round(price, 2) and the broker's average).
PRICE_EPS = 0.005


def reconcile(intent: Intent, update: OrderUpdate,
              price_eps: float = PRICE_EPS) -> Correction:
    """(what we assumed, what the broker says) -> what the ledger must do.

    Only terminal updates produce a correction; see the module docstring on why
    PART_TRADED is left to the terminal one that follows it.
    """
    if not update.is_terminal:
        return Correction("confirm", reason=f"still working ({update.status})")

    filled = update.filled_qty
    # A TRADED update that reports no quantity is Dhan not populating the field
    # rather than a zero fill — treating it as a void would delete a position
    # that really exists. Trust the status, keep our quantity.
    if update.status == "TRADED" and filled <= 0:
        filled = intent.qty

    if filled <= 0:
        return Correction("void", reason=(
            f"{update.status.lower()} with nothing filled"
            + (f": {update.remarks}" if update.remarks else "")))

    price = update.avg_price if update.avg_price else intent.price
    repriced = abs(price - intent.price) > price_eps
    resized = abs(filled - intent.qty) > 1e-9
    if not repriced and not resized:
        return Correction("confirm", reason="filled as booked")

    bits = []
    if repriced:
        bits.append(f"price {intent.price:g} -> {price:g}")
    if resized:
        bits.append(f"qty {intent.qty:g} -> {filled:g}")
    if update.status != "TRADED":
        bits.append(f"partial fill then {update.status.lower()}")
    return Correction("amend", price=price, qty=filled, reason=", ".join(bits))


# ---------------------------------------------------------------------------
# in-flight intents
# ---------------------------------------------------------------------------

class FillReconciler:
    """Tracks placed orders until the broker says what happened to them.

    The ledger mutation itself is `on_correction(intent, correction)` — this
    class decides WHAT changed, LiveContext decides how to apply it, so the
    whole decision path stays testable without a ledger.
    """

    # An order still in flight after this long is a real risk: we are showing a
    # position the broker may never have opened (or, worse, showing flat after
    # an exit that never went through).
    STALE_S = 300.0
    # Terminal order ids are remembered so a duplicate/replayed update is not
    # applied twice. Bounded so a long session can't grow it without limit.
    _SETTLED_MAX = 4000

    def __init__(self, on_correction: Callable[[Intent, Correction], None],
                 on_event: Callable[[str, str, str], None]):
        self._on_correction = on_correction
        self._on_event = on_event
        self._pending: dict[str, Intent] = {}
        self._settled: dict[str, str] = {}      # order_id -> action taken
        self._stale_flagged: set = set()
        self.connected = False
        self.last_update: Optional[datetime] = None
        self.applied = 0
        self.corrected = 0
        self.unparsed = 0

    # -- registration --------------------------------------------------------

    def track(self, intent: Intent) -> None:
        """Register an order the ledger has already booked optimistically."""
        if not intent.order_id:
            # A dry-run/blocked order with no id can never be reconciled. Say
            # so once rather than silently tracking nothing.
            self._on_event("warn", intent.sid,
                           f"no order id for {intent.label} — "
                           f"this fill cannot be reconciled")
            return
        self._pending[intent.order_id] = intent

    # An order is deliberately NOT forgotten when its strategy is stopped: an
    # entry that never confirmed is still an order that may be live at the
    # broker, and that is exactly when you want the sweep to say so. Corrections
    # for an undeployed strategy are handled by LiveRunner._dispatch_correction.

    # -- updates -------------------------------------------------------------

    def apply(self, msg) -> Optional[tuple]:
        """Feed one raw broker message. Returns (intent, correction) when a
        tracked order reached a terminal state, else None."""
        update = parse_order_update(msg)
        if update is None:
            self.unparsed += 1
            self._on_event("warn", "",
                           f"unparseable order update: {msg!r:.300}")
            return None
        self.last_update = datetime.now(IST).replace(tzinfo=None)
        self.applied += 1
        intent = self._pending.get(update.order_id)
        if intent is None:
            if update.order_id in self._settled:
                return None          # duplicate/replay of one we already applied
            # Not ours: super-order SL/target legs report under their own ids,
            # and so does anything placed from the Dhan app. Not an error.
            return None
        if not update.is_terminal:
            return None
        del self._pending[update.order_id]
        self._stale_flagged.discard(update.order_id)
        corr = reconcile(intent, update)
        self._remember(update.order_id, corr.action)
        if corr.changes_ledger:
            self.corrected += 1
            level = "error" if corr.action == "void" else "warn"
            self._on_event(level, intent.sid,
                           f"fill reconciled [{intent.kind} {intent.label}] "
                           f"{corr.action}: {corr.reason}")
        try:
            self._on_correction(intent, corr)
        except Exception as e:
            self._on_event("error", intent.sid,
                           f"could not apply fill correction for "
                           f"{intent.label}: {e!r}")
        return intent, corr

    def _remember(self, order_id: str, action: str) -> None:
        if len(self._settled) >= self._SETTLED_MAX:
            for k in list(self._settled)[:self._SETTLED_MAX // 2]:
                del self._settled[k]
        self._settled[order_id] = action

    # -- the orders nobody ever told us about --------------------------------

    def sweep(self, now: Optional[datetime] = None) -> list:
        """Orders with no terminal update after STALE_S. These are the
        dangerous ones — the ledger is asserting something the broker has not
        confirmed — so each is reported ONCE, loudly, and left in flight in
        case the update is merely late."""
        now = now or datetime.now(IST).replace(tzinfo=None)
        stale = []
        for oid, intent in list(self._pending.items()):
            if (now - intent.placed_ts).total_seconds() < self.STALE_S:
                continue
            stale.append(intent)
            if oid in self._stale_flagged:
                continue
            self._stale_flagged.add(oid)
            self._on_event("error", intent.sid,
                           f"UNCONFIRMED {intent.kind} [{intent.label}] — no "
                           f"terminal update {self.STALE_S:.0f}s after placing "
                           f"order {oid}; the LIVE ledger may not match the "
                           f"broker. Check the order book.")
        return stale

    def status(self) -> dict:
        """Health surface for /live/status. `reconciled` false means the ledger
        is running as-placed — not wrong, but unverified."""
        return {
            "reconciled": bool(self.connected),
            "connected": self.connected,
            "pending": len(self._pending),
            "applied": self.applied,
            "corrected": self.corrected,
            "unparsed": self.unparsed,
            "last_update": self.last_update.isoformat(sep=" ",
                                                     timespec="seconds")
            if self.last_update else None,
            "pending_orders": [
                {"order_id": i.order_id, "kind": i.kind, "label": i.label,
                 "placed": i.placed_ts.isoformat(sep=" ", timespec="seconds")}
                for i in self._pending.values()],
        }


# ---------------------------------------------------------------------------
# WebSocket driver (thread-isolated, mirrors engines/feed.LiveFeed)
# ---------------------------------------------------------------------------

class LiveOrderFeed:
    """Runs a dhanhq OrderUpdate socket on its own thread and pushes each raw
    message to `on_message` (invoked on `app_loop`).

    The SDK's connect_order_update() opens the socket, authenticates, and then
    iterates messages until the connection drops — at which point it simply
    returns. There is no retry inside it, so the retry loop is here.
    """

    IDLE_BACKOFF_S = 30      # ceiling for reconnect backoff

    def __init__(self, context_factory: Callable[[], object],
                 on_message: Callable[[object], None],
                 on_event: Callable[[str, str], None]):
        self._context_factory = context_factory
        self._on_message = on_message
        self._on_event = on_event
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._app_loop: Optional[asyncio.AbstractEventLoop] = None
        self._backoff_s = 1.0
        self.connected = False

    def start(self, app_loop: asyncio.AbstractEventLoop) -> None:
        if self._running:
            return
        self._running = True
        self._app_loop = app_loop
        self._thread = threading.Thread(target=self._thread_main,
                                        name="dhan-orderupdate", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=3)

    # -- thread internals ----------------------------------------------------

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._driver())
        finally:
            loop.close()

    async def _driver(self) -> None:
        # WHOLE BODY INSIDE THE TRY (tests/test_loop_resilience.py enforces it
        # structurally): a driver that dies here stops correcting the ledger
        # while every other indicator stays green — an unreconciled LIVE book
        # that still looks healthy is precisely the failure this must not have.
        while self._running:
            try:
                await self._session()
            except Exception as e:
                self._event("warn", f"order update socket error: {e!r}")
            finally:
                self.connected = False
            await self._sleep_backoff()

    async def _session(self) -> None:
        """One connect-and-listen. The SDK's connect_order_update() authenticates
        and iterates messages until the socket drops, then simply returns."""
        from dhanhq import OrderUpdate as SdkOrderUpdate
        sock = SdkOrderUpdate(self._context_factory())
        sock.on_update = self._bridge
        self.connected = True
        self._backoff_s = 1.0
        self._event("info", "order update socket connected")
        await sock.connect_order_update()
        self._event("warn", "order update socket closed")

    async def _sleep_backoff(self) -> None:
        """Exponential backoff, waited in slices so stop() is not held up for a
        full backoff window."""
        wait = self._backoff_s
        self._backoff_s = min(self._backoff_s * 2, self.IDLE_BACKOFF_S)
        end = time.monotonic() + wait
        while self._running and time.monotonic() < end:
            await asyncio.sleep(0.25)

    def _bridge(self, msg) -> None:
        """Called on the socket thread — hand the message to the app loop."""
        loop = self._app_loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._on_message, msg)
        except Exception:
            pass

    def _event(self, level: str, msg: str) -> None:
        try:
            self._on_event(level, msg)
        except Exception:
            pass
