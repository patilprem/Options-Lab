"""
Live Execution Adapter + Kill Switch (M8) — behind hard safety gates
====================================================================
Mirrors PaperContext but routes enter/exit through Dhan's ORDER APIs and writes
to the LIVE ledgers. It is deliberately conservative and gated; nothing here can
place a real order unless EVERY gate is open (see LiveGate).

Defense in depth — a real order requires ALL of:
  1. setting live_enabled == "on"        (master switch; default off)
  2. setting live_dry_run  == "off"      (default on -> DryRunOrderClient logs
                                           orders instead of placing them)
  3. running on the whitelisted STATIC IP (Dhan rejects otherwise, DH-911)
  4. per-strategy checklist acknowledged (live-modal, enforced at deploy)
  5. market hours, lots <= live_max_lots, and risk caps pass

Invariants honored:
  * PAPER and LIVE are separate ledgers (mode="LIVE"); never mixed.
  * Paper code paths are untouched — this is a parallel implementation.
  * Orders use place_super_order so the STOP-LOSS lives on Dhan's servers and
    protects the position even if this process dies.

NOTE: full fill reconciliation (OrderUpdate WS, partials, broker position sync)
is the next increment; positions here are recorded as-placed. Live behaviour
MUST be verified on the VPS during market hours before real capital is used.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Optional

from app.core import registry
from app.core.contract import (Action, Bar, Context, LegSpec, OptionQuote,
                               Position, Strategy)
from app.data.dhan_client import UNDERLYINGS
from app.engines import fills as F
from app.engines import margin as M
from app.engines import risk as R
from app.engines.backtest import lot_size_on

IST = timezone(timedelta(hours=5, minutes=30))
_ORDER_TYPE = "LIMIT"
_PRODUCT = "INTRADAY"


# ---------------------------------------------------------------------------
# settings gates
# ---------------------------------------------------------------------------

def live_enabled() -> bool:
    return registry.setting("live_enabled", "off") == "on"


def dry_run() -> bool:
    return registry.setting("live_dry_run", "on") != "off"


def max_lots() -> int:
    try:
        return max(1, int(registry.setting("live_max_lots", "1")))
    except ValueError:
        return 1


def checklist_ack(sid: str) -> bool:
    return registry.setting(f"live_checklist_ack:{sid}", "no") == "yes"


# ---------------------------------------------------------------------------
# order clients
# ---------------------------------------------------------------------------

class DryRunOrderClient:
    """Safe default: records intended orders instead of sending them. Used
    whenever live isn't fully enabled, and in tests."""
    def __init__(self):
        self.orders: list[dict] = []

    def place_super_order(self, **kw):
        self.orders.append({"kind": "super", **kw})
        return {"status": "success", "data": {"orderId": f"dry-{len(self.orders)}"},
                "dry_run": True}

    def cancel_super_order(self, order_id, order_leg="ENTRY_LEG"):
        self.orders.append({"kind": "cancel", "order_id": order_id, "leg": order_leg})
        return {"status": "success", "dry_run": True}

    def place_order(self, **kw):
        self.orders.append({"kind": "order", **kw})
        return {"status": "success", "data": {"orderId": f"dry-{len(self.orders)}"},
                "dry_run": True}

    def kill_switch(self, action):
        self.orders.append({"kind": "kill_switch", "action": action})
        return {"status": "success", "dry_run": True}

    def status_kill_switch(self):
        return {"status": "success", "data": {"killSwitchStatus": "unknown"}, "dry_run": True}


class RealOrderClient:
    """Thin pass-through to the dhanhq SDK order methods. Only constructed when
    live is fully enabled AND dry-run is off."""
    def __init__(self, client):
        self._c = client

    def place_super_order(self, **kw):
        return self._c.place_super_order(**kw)

    def cancel_super_order(self, order_id, order_leg="ENTRY_LEG"):
        return self._c.cancel_super_order(order_id=order_id, order_leg=order_leg)

    def place_order(self, **kw):
        return self._c.place_order(**kw)

    def kill_switch(self, action):
        return self._c.kill_switch(action)

    def status_kill_switch(self):
        return self._c.status_kill_switch()


def make_order_client():
    """DryRun unless live is enabled AND dry-run is off. Even then, real orders
    still require the static IP (Dhan enforces)."""
    if live_enabled() and not dry_run():
        try:
            from app.data import dhan_client
            return RealOrderClient(dhan_client.get_client())
        except Exception as e:
            registry.record_event("error", "live",
                                  f"real order client unavailable, using dry-run: {e!r}")
    return DryRunOrderClient()


# ---------------------------------------------------------------------------
# kill switch
# ---------------------------------------------------------------------------

class KillSwitch:
    def __init__(self, client):
        self._c = client

    def arm(self):
        registry.set_setting("live_kill_armed", "yes")
        registry.record_event("warn", "live", "KILL SWITCH armed — trading disabled")
        return self._c.kill_switch("activate")

    def disarm(self):
        registry.set_setting("live_kill_armed", "no")
        registry.record_event("info", "live", "kill switch released")
        return self._c.kill_switch("deactivate")

    def armed(self) -> bool:
        return registry.setting("live_kill_armed", "no") == "yes"


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------

class LiveGate:
    """Blocks entries unless every non-account condition passes. Returns
    (ok, reason)."""

    @staticmethod
    def market_open(now: datetime) -> bool:
        return now.weekday() < 5 and dtime(9, 15) <= now.time() <= dtime(15, 30)

    @classmethod
    def can_enter(cls, legs: list[LegSpec], now: datetime) -> tuple[bool, str]:
        if not live_enabled():
            return False, "live trading disabled (master switch off)"
        if registry.setting("live_kill_armed", "no") == "yes":
            return False, "kill switch armed"
        if not cls.market_open(now):
            return False, "outside market hours"
        cap = max_lots()
        if any(leg.lots > cap for leg in legs):
            return False, f"lots exceed live_max_lots={cap}"
        return True, ""


# ---------------------------------------------------------------------------
# live context (mirrors PaperContext's Context surface)
# ---------------------------------------------------------------------------

class LiveContext(Context):
    def __init__(self, record: registry.StrategyRecord, underlying: str, hub,
                 interval: int = 5, client=None):
        self.rec = record
        self.underlying = underlying
        self.hub = hub
        self.interval = int(interval)
        self.fee_cfg, self.slip_cfg = F.FeeConfig(), F.SlippageConfig()
        self._client = client or make_order_client()
        self.kill = KillSwitch(self._client)
        self.paused = record.state != registry.State.RUNNING
        self._bars: list[Bar] = []
        self._open: list[Position] = []
        self.closed_today: list[Position] = []
        self._margin_used = 0.0
        self._realized_today = 0.0
        self._fees_today = 0.0
        self._day = None            # trading date the counters belong to

    # -- engine wiring -------------------------------------------------------
    def _roll_day(self, ts: datetime) -> None:
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
        # NOTE: SL/target are enforced BROKER-SIDE by the super order; we do not
        # re-enforce locally (unlike paper) to avoid double exits.

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        if paused and self.rec.square_off_on_pause:
            self.exit_all()

    # -- Context surface -----------------------------------------------------
    @property
    def now(self) -> datetime:
        return self._bars[-1].ts if self._bars else datetime.now(IST).replace(tzinfo=None)

    @property
    def lot_size(self) -> int:
        return lot_size_on(self.underlying, self.now.date())

    @property
    def spot(self) -> float:
        return self._bars[-1].close if self._bars else 0.0

    def option(self, leg: LegSpec) -> Optional[OptionQuote]:
        return self.hub.quote(self.underlying, self.now, leg)

    def history(self, n: int) -> list[Bar]:
        return self._bars[-n:]

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

    # -- order routing -------------------------------------------------------
    def enter(self, legs: list[LegSpec], tag: str = "",
              sl_pct=None, target_pct=None) -> bool:
        if self.paused:
            self.log(f"enter blocked: strategy paused ({tag})")
            return False
        ok, reason = LiveGate.can_enter(legs, self.now)
        if not ok:
            self.log(f"enter blocked: {reason} ({tag})")
            return False
        quotes = [(leg, self.option(leg)) for leg in legs]
        if any(q is None or not q.security_id for _, q in quotes):
            self.log(f"enter blocked: missing live quote/security_id ({tag})")
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

        placed = 0
        for leg, q in quotes:
            units = leg.lots * self.lot_size
            price = q.ask if (leg.action == Action.BUY and q.ask) else \
                    q.bid if (leg.action == Action.SELL and q.bid) else q.ltp
            qty = units if leg.action == Action.BUY else -units
            sl, tgt = F.levels_for(price, qty, sl_pct, target_pct)
            resp = self._client.place_super_order(
                security_id=q.security_id, exchange_segment=seg,
                transaction_type=leg.action.value, quantity=units,
                order_type=_ORDER_TYPE, product_type=_PRODUCT, price=round(price, 2),
                stopLossPrice=round(sl, 2) if sl else 0.0,
                targetPrice=round(tgt, 2) if tgt else 0.0,
                tag=(tag or leg.tag)[:24])
            if resp.get("status") != "success":
                self.log(f"ORDER REJECTED ({leg.tag}): {resp.get('remarks')}")
                continue
            oid = str((resp.get("data") or {}).get("orderId", ""))
            fees = F.charges(price * units, leg.action, self.fee_cfg)
            pos = Position(
                id=oid or str(uuid.uuid4())[:8], leg=leg, underlying=self.underlying,
                expiry=q.expiry, strike=q.strike, qty=qty, entry_price=round(price, 2),
                entry_ts=self.now, mtm_price=round(price, 2), fees_paid=fees,
                tag=tag or leg.tag, stop_loss=sl, target=tgt)
            self._open.append(pos)
            self._fees_today += fees
            self._blotter(pos, leg.action.value, price, fees, "entry",
                          dry=bool(resp.get("dry_run")))
            placed += 1
        if placed:
            self._margin_used += est
        return placed > 0

    def set_levels(self, position_id: str, stop_loss=None, target=None) -> bool:
        # TODO(live): modify_super_order on the SL/target legs. Scaffolded local update.
        for p in self._open:
            if p.id == position_id and p.is_open:
                if stop_loss is not None:
                    p.stop_loss = stop_loss
                if target is not None:
                    p.target = target
                return True
        return False

    def exit(self, position_id: str) -> bool:
        for p in self._open:
            if p.id == position_id and p.is_open:
                self._square(p, reason="manual")
                return True
        return False

    def exit_all(self) -> None:
        for p in list(self.positions):
            self._square(p, reason="manual")

    def _square(self, p: Position, reason: str = "manual") -> None:
        seg = UNDERLYINGS.get(self.underlying, {}).get("fno_segment", "NSE_FNO")
        # cancel the resting super order, then send an opposite squaring order
        try:
            self._client.cancel_super_order(p.id, "ENTRY_LEG")
        except Exception:
            pass
        # Quote the ACTUAL contract held — the leg-key quote re-anchors to the
        # current ATM, so it could route this REAL exit order to the wrong
        # security_id after spot has moved.
        q = self.hub.quote_position(self.underlying, self.now, p)
        exit_action = Action.SELL if p.qty > 0 else Action.BUY
        price = (q.ltp if q else p.mtm_price)
        resp = self._client.place_order(
            security_id=(q.security_id if q else None), exchange_segment=seg,
            transaction_type=exit_action.value, quantity=abs(p.qty),
            order_type=_ORDER_TYPE, product_type=_PRODUCT, price=round(price, 2),
            tag=f"exit_{reason}"[:24])
        fees = F.charges(price * abs(p.qty), exit_action, self.fee_cfg)
        p.exit_price = round(price, 2)
        p.exit_ts = self.now
        p.exit_reason = reason
        p.fees_paid += fees
        self._fees_today += fees
        self._realized_today += p.realized_pnl
        self.closed_today.append(p)
        self._open.remove(p)
        self._blotter(p, exit_action.value, price, fees, reason,
                      dry=bool(resp.get("dry_run")))

    # -- ledger --------------------------------------------------------------
    def _blotter(self, p: Position, side: str, price: float, fees: float,
                 reason: str, dry: bool = False) -> None:
        opt = "CE" if p.leg.option_type.value == "CALL" else "PE"
        registry.record_event("info", "live",
                              f"{'[DRY] ' if dry else ''}{side} {abs(p.qty)} "
                              f"{p.underlying} {p.strike:g} {opt} @ {price} ({reason})",
                              self.rec.id)
        registry.record_trade(self.rec.id, "LIVE", {
            "ts": self.now.isoformat(sep=" ", timespec="seconds"),
            "contract": f"{p.underlying} {p.strike:g} {opt}".upper(),
            "side": side, "qty": abs(p.qty), "price": round(price, 2),
            "fees": round(fees, 2), "reason": reason, "tag": p.tag,
            "dry_run": dry,
        })

    def log(self, msg: str) -> None:
        print(f"[LIVE {self.rec.id} {self.now:%H:%M}] {msg}")
        lvl = "warn" if "blocked" in msg or "REJECT" in msg else "info"
        registry.record_event(lvl, "live", msg, self.rec.id)

    def persist_day(self) -> None:
        """Idempotent (same contract as PaperContext.persist_day): always writes
        the FULL day totals. Counters roll in _roll_day on a date change, so a
        repeated call can never zero out the day it just saved.
        No-bar guard: with no bars, `now` is wall-clock — never stamp a
        phantom row for a closed-market day."""
        if not self._bars:
            return
        unreal = sum(p.unrealized_pnl for p in self.positions)
        day = self.now.date().isoformat()
        base = registry.prev_equity(self.rec.id, day, "LIVE")
        if base is None:
            base = self.rec.allocated_capital
        registry.save_day(self.rec.id, "LIVE", day,
                          round(self._realized_today, 2), round(unreal, 2),
                          round(self._fees_today, 2),
                          round(base + self._realized_today + unreal, 2))


# ---------------------------------------------------------------------------
# live runner (parallel to PaperRunner; writes LIVE ledgers only)
# ---------------------------------------------------------------------------

class LiveRunner:
    """Owns live strategy tasks. Separate from PaperRunner by design so paper
    code paths are never touched. Deployment is gated (live_enabled + per-strategy
    checklist) at the API layer."""

    def __init__(self, hub):
        self.hub = hub
        self.contexts: dict[str, LiveContext] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    async def deploy(self, record: registry.StrategyRecord, strategy: Strategy) -> None:
        meta = strategy.meta()
        interval = int(meta.timeframe)
        ctx = LiveContext(record, meta.underlying, self.hub, interval)
        self.contexts[record.id] = ctx
        self.hub.register(meta.underlying, interval)
        await self.hub.ensure_started()
        registry.record_event("warn", "live",
                              f"LIVE deploy ({'DRY-RUN' if dry_run() else 'REAL ORDERS'})",
                              record.id)
        self.tasks[record.id] = asyncio.create_task(self._loop(record.id, strategy, ctx))

    async def _loop(self, sid: str, strategy: Strategy, ctx: LiveContext) -> None:
        q = self.hub.subscribe()
        strategy.on_start(ctx)
        while True:
            kind, underlying, interval, bar = await q.get()
            if underlying != ctx.underlying:
                continue
            if interval is not None and interval != ctx.interval:
                continue
            if kind == "eod" or (bar and bar.ts.time() >= dtime(15, 25)):
                if bar:
                    ctx.push_bar(bar)
                ctx.exit_all()
                strategy.on_day_end(ctx)
                ctx.persist_day()
                if kind == "eod":
                    continue
            ctx.push_bar(bar)
            try:
                strategy.on_bar(ctx, bar)
            except Exception as e:
                registry.record_event("error", "live",
                                      f"Strategy crashed (auto-paused): {e!r}", sid)
                ctx.set_paused(True)
                registry.transition(sid, registry.State.DEPLOYED_PAUSED)
            self.enforce_risk()

    def enforce_risk(self) -> None:
        """Live risk: pause strategies over their cap; on a PORTFOLIO breach, arm
        the broker kill switch and square everything."""
        ev = R.evaluate(self.contexts)
        for b in ev["strategy_breaches"]:
            ctx = self.contexts.get(b["sid"])
            if ctx and not ctx.paused:
                ctx.set_paused(True)   # square_off_on_pause squares if set
                registry.record_event("error", "risk",
                    f"LIVE daily loss cap ₹{b['cap']:,.0f} hit; strategy paused", b["sid"])
                try:
                    registry.transition(b["sid"], registry.State.DEPLOYED_PAUSED)
                except Exception:
                    pass
        if ev["portfolio_breach"]:
            self.kill_all(f"portfolio max daily loss ₹{ev['max_loss']:,.0f} breached")

    def kill_all(self, reason: str) -> None:
        """Arm the broker kill switch and flatten every live position."""
        registry.record_event("error", "live", f"KILL: {reason}; flattening all")
        for sid, ctx in list(self.contexts.items()):
            try:
                ctx.exit_all()
                ctx.kill.arm()
                ctx.set_paused(True)
                registry.transition(sid, registry.State.DEPLOYED_PAUSED)
            except Exception as e:
                registry.record_event("error", "live", f"kill_all error {sid}: {e!r}")

    async def stop(self, sid: str) -> None:
        ctx = self.contexts.get(sid)
        if ctx:
            ctx.exit_all()
            ctx.persist_day()
        if sid in self.tasks:
            self.tasks[sid].cancel()
            del self.tasks[sid]
        self.contexts.pop(sid, None)
