"""M8 fill reconciliation: the LIVE ledger vs what the broker actually did.

LiveContext books a position the instant an order is ACCEPTED, at the LIMIT
price it asked for. That is intent, not fact — a limit order can rest unfilled,
fill partially, fill at a better price, or be rejected after acceptance. These
cover the whole path: parsing Dhan's order_alert payload, deciding what
diverged, and landing the correction on positions, fees and realized P&L.

Offline: no SDK, no socket, no credentials. The dhanhq import lives inside
LiveOrderFeed._session, which none of this touches.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.core import registry
from app.core.contract import (Action, Bar, ExpiryKind, LegSpec, OptionQuote,
                               OptionType)
from app.engines import live as L
from app.engines import order_updates as OU

FRI_10AM = datetime(2026, 7, 10, 10, 0)


# ---------------------------------------------------------------------------
# parsing (VPS-pending: field names beyond orderNo/status are not in the SDK,
# so the parser accepts every spelling Dhan's REST order object uses)
# ---------------------------------------------------------------------------

def _alert(**data):
    return {"Type": "order_alert", "Data": data}


def test_parses_the_sdk_shaped_payload():
    u = OU.parse_order_update(_alert(
        orderNo="112233", status="TRADED", filledQty="75",
        averageTradedPrice="146.25"))
    assert (u.order_id, u.status, u.filled_qty, u.avg_price) == \
        ("112233", "TRADED", 75.0, 146.25)
    assert u.is_terminal


def test_parses_a_bare_body_without_the_envelope():
    u = OU.parse_order_update({"orderId": "9", "orderStatus": "Rejected"})
    assert u.order_id == "9" and u.status == "REJECTED"


def test_accepts_alternative_field_spellings():
    u = OU.parse_order_update(_alert(
        order_id="7", order_status="part traded", traded_quantity=25,
        avg_price=100.5, omsErrorDescription="insufficient margin"))
    assert u.status == "PART_TRADED"
    assert u.filled_qty == 25.0 and u.avg_price == 100.5
    assert u.remarks == "insufficient margin"


def test_a_non_order_message_is_not_an_update():
    assert OU.parse_order_update({"Type": "market_status"}) is None
    assert OU.parse_order_update("hello") is None
    assert OU.parse_order_update(_alert(status="TRADED")) is None   # no id


def test_an_unknown_status_is_never_terminal():
    """Acting on a status we don't understand is worse than leaving the order
    in flight for the sweep to flag."""
    u = OU.parse_order_update(_alert(orderNo="1", status="SOMETHING_NEW"))
    assert u.status == "UNKNOWN" and not u.is_terminal


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------

def _intent(kind="entry", price=145.0, qty=75.0, fees=12.0):
    return OU.Intent(order_id="1", sid="s1", kind=kind, position_id="p1",
                     price=price, qty=qty, fees=fees, placed_ts=FRI_10AM,
                     label="NIFTY 23950 CE")


def _upd(status, qty=0.0, price=None, remarks=""):
    return OU.OrderUpdate("1", status, qty, price, remarks)


def test_a_working_order_is_left_alone():
    c = OU.reconcile(_intent(), _upd("PENDING"))
    assert c.action == "confirm" and not c.changes_ledger


def test_a_partial_is_left_to_the_terminal_update():
    """PART_TRADED arrives while the order is still working and its filled
    quantity keeps moving; correcting on both would double-apply."""
    assert OU.reconcile(_intent(), _upd("PART_TRADED", 25, 145.0)).action \
        == "confirm"


def test_a_clean_fill_needs_no_correction():
    assert OU.reconcile(_intent(), _upd("TRADED", 75, 145.0)).action == "confirm"


def test_a_paise_difference_is_rounding_not_slippage():
    assert OU.reconcile(_intent(), _upd("TRADED", 75, 145.002)).action \
        == "confirm"


def test_a_different_average_price_is_amended():
    c = OU.reconcile(_intent(), _upd("TRADED", 75, 146.25))
    assert c.action == "amend" and c.price == 146.25 and c.qty == 75
    assert "145 -> 146.25" in c.reason


def test_a_short_fill_is_amended():
    c = OU.reconcile(_intent(), _upd("TRADED", 50, 145.0))
    assert c.action == "amend" and c.qty == 50


def test_a_rejection_voids_the_position():
    c = OU.reconcile(_intent(), _upd("REJECTED", 0, remarks="RMS blocked"))
    assert c.action == "void" and "RMS blocked" in c.reason


def test_a_cancel_after_a_partial_keeps_what_filled():
    c = OU.reconcile(_intent(), _upd("CANCELLED", 25, 145.0))
    assert c.action == "amend" and c.qty == 25
    assert "cancelled" in c.reason


def test_a_traded_update_with_no_quantity_keeps_ours():
    """Dhan not populating the field is not a zero fill — voiding here would
    delete a position that really exists."""
    assert OU.reconcile(_intent(), _upd("TRADED", 0, 145.0)).action == "confirm"


# ---------------------------------------------------------------------------
# the in-flight book
# ---------------------------------------------------------------------------

def _recon():
    seen, events = [], []
    r = OU.FillReconciler(on_correction=lambda i, c: seen.append((i, c)),
                          on_event=lambda lvl, sid, m: events.append((lvl, m)))
    return r, seen, events


def test_a_terminal_update_reaches_the_ledger():
    r, seen, _ = _recon()
    r.track(_intent())
    r.apply(_alert(orderNo="1", status="TRADED", filledQty=75,
                   averageTradedPrice=146.0))
    assert len(seen) == 1 and seen[0][1].action == "amend"


def test_an_order_we_did_not_place_is_ignored_quietly():
    """Super-order SL/target legs report under their own ids, and so does
    anything placed from the Dhan app. Not an error."""
    r, seen, events = _recon()
    r.track(_intent())
    r.apply(_alert(orderNo="999", status="TRADED", filledQty=75))
    assert seen == [] and events == []


def test_a_replayed_update_is_not_applied_twice():
    r, seen, _ = _recon()
    r.track(_intent())
    msg = _alert(orderNo="1", status="TRADED", filledQty=50, averageTradedPrice=1.0)
    r.apply(msg)
    r.apply(msg)
    assert len(seen) == 1


def test_an_unparseable_message_is_counted_and_logged():
    """The thing to watch for on the VPS: a payload shape the parser can't
    read means corrections are silently not happening."""
    r, _, events = _recon()
    r.apply({"Type": "order_alert", "Data": {"nothing": "useful"}})
    assert r.unparsed == 1
    assert any("unparseable" in m for _, m in events)


def test_an_order_with_no_id_says_so_rather_than_tracking_nothing():
    r, _, events = _recon()
    r.track(OU.Intent("", "s1", "entry", "p1", 1.0, 1.0, 0.0, FRI_10AM, "X"))
    assert any("cannot be reconciled" in m for _, m in events)


def test_the_sweep_flags_an_unconfirmed_order_once():
    r, _, events = _recon()
    r.track(_intent())
    late = FRI_10AM + timedelta(seconds=OU.FillReconciler.STALE_S + 1)
    assert len(r.sweep(late)) == 1
    assert len(r.sweep(late)) == 1, "still stale, but must not re-alarm"
    errs = [m for lvl, m in events if lvl == "error"]
    assert len(errs) == 1 and "UNCONFIRMED" in errs[0]


def test_a_confirmed_order_is_never_swept():
    r, _, events = _recon()
    r.track(_intent())
    r.apply(_alert(orderNo="1", status="TRADED", filledQty=75, averageTradedPrice=145.0))
    late = FRI_10AM + timedelta(seconds=OU.FillReconciler.STALE_S + 1)
    assert r.sweep(late) == []


def test_status_reports_an_unreconciled_ledger():
    r, _, _ = _recon()
    r.track(_intent())
    st = r.status()
    assert st["reconciled"] is False and st["pending"] == 1
    assert st["pending_orders"][0]["order_id"] == "1"


# ---------------------------------------------------------------------------
# landing it on a real LIVE ledger
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "l.db")
    registry.init_db()
    registry.set_setting("live_enabled", "on")
    return tmp_path


class FakeHub:
    def __init__(self, q):
        self._q = q

    def quote(self, u, ts, leg):
        return self._q

    def quote_position(self, u, ts, pos):
        return self._q

    def market_client(self):
        return None


def _quote():
    return OptionQuote(ts=FRI_10AM, underlying="NIFTY", expiry=date(2026, 7, 16),
                       strike=23950.0, option_type=OptionType.CALL, ltp=145.0,
                       bid=145.0, ask=146.0, security_id="51366")


LEG = [LegSpec(OptionType.CALL, Action.SELL, 0, ExpiryKind.WEEKLY, 0, 1, "ce")]
# NIFTY's lot size in force on 2026-07-10 (backtest.LOT_HISTORY). _ctx asserts
# it, so a future exchange circular fails here with a reason rather than
# surfacing as a mysterious resize three tests later.
UNITS = 65


def _ctx():
    rec = registry.create("Live", "code")
    registry.transition(rec.id, registry.State.VALIDATED)
    registry.transition(rec.id, registry.State.DEPLOYED_PAUSED)
    registry.allocate(rec.id, 3_000_000)
    registry.transition(rec.id, registry.State.RUNNING)
    ctx = L.LiveContext(registry.get(rec.id), "NIFTY", FakeHub(_quote()), 5,
                        client=L.DryRunOrderClient())
    ctx.push_bar(Bar(FRI_10AM, 23950, 23960, 23940, 23950))
    ctx.enter(LEG, tag="t")
    assert abs(ctx.positions[0].qty) == UNITS, "lot size moved; update UNITS"
    return ctx


def _order_id(ctx):
    return ctx.reconciler.status()["pending_orders"][-1]["order_id"]


def _blotter(ctx, order_id):
    rows = registry.trades_for(ctx.rec.id, FRI_10AM.date().isoformat(), "LIVE")
    return [r for r in rows if r.get("order_id") == order_id]


def test_an_entry_fill_at_a_worse_price_corrects_the_position(db):
    ctx = _ctx()
    oid = _order_id(ctx)
    pos = ctx.positions[0]
    booked_fees = ctx._fees_today
    ctx.reconciler.apply(_alert(orderNo=oid, status="TRADED", filledQty=UNITS,
                                averageTradedPrice=142.0))
    assert pos.entry_price == 142.0
    assert ctx._fees_today != booked_fees, "fees must follow the real price"
    assert _blotter(ctx, oid)[0]["price"] == 142.0
    assert _blotter(ctx, oid)[0]["requested_price"] == 145.0


def test_a_short_entry_fill_resizes_the_position(db):
    ctx = _ctx()
    oid = _order_id(ctx)
    ctx.reconciler.apply(_alert(orderNo=oid, status="TRADED", filledQty=UNITS - 15,
                                averageTradedPrice=145.0))
    assert abs(ctx.positions[0].qty) == UNITS - 15


def test_a_rejected_entry_removes_the_phantom_position(db):
    ctx = _ctx()
    oid = _order_id(ctx)
    ctx.reconciler.apply(_alert(orderNo=oid, status="REJECTED",
                                omsErrorDescription="RMS: margin"))
    assert ctx.positions == []
    assert ctx._fees_today == 0.0, "fees for a trade that never happened"
    assert ctx._margin_used == 0.0, "margin must be released"
    assert _blotter(ctx, oid)[0]["voided"] is True


def test_an_exit_fill_at_a_different_price_corrects_realized_pnl(db):
    ctx = _ctx()
    ctx.reconciler.apply(_alert(orderNo=_order_id(ctx), status="TRADED",
                                filledQty=UNITS, averageTradedPrice=145.0))
    ctx.exit_all(reason="signal")
    oid = _order_id(ctx)
    pos = ctx.closed_today[0]
    ctx.reconciler.apply(_alert(orderNo=oid, status="TRADED", filledQty=UNITS,
                                averageTradedPrice=120.0))
    assert pos.exit_price == 120.0
    # sold at 145, bought back at 120 -> 25 points x 75 units, less fees
    assert ctx._realized_today == pytest.approx(pos.realized_pnl)
    assert pos.realized_pnl == pytest.approx(25 * UNITS - pos.fees_paid)


def test_a_rejected_exit_reopens_the_position(db):
    """THE dangerous divergence: the ledger reads flat while the broker still
    holds the position."""
    ctx = _ctx()
    ctx.exit_all(reason="signal")
    assert ctx.positions == []
    oid = _order_id(ctx)
    ctx.reconciler.apply(_alert(orderNo=oid, status="REJECTED",
                                omsErrorDescription="RMS blocked"))
    assert len(ctx.positions) == 1, "the position is still live at the broker"
    assert ctx.positions[0].is_open
    assert ctx.closed_today == []
    assert ctx._realized_today == 0.0
    assert _blotter(ctx, oid)[0]["voided"] is True


def test_a_partial_exit_splits_the_position(db):
    """25 of 65 filled: the closed part books its real P&L, the remaining 40
    stay open instead of silently vanishing from the ledger."""
    ctx = _ctx()
    ctx.reconciler.apply(_alert(orderNo=_order_id(ctx), status="TRADED",
                                filledQty=UNITS, averageTradedPrice=145.0))
    ctx.exit_all(reason="signal")
    oid = _order_id(ctx)
    ctx.reconciler.apply(_alert(orderNo=oid, status="CANCELLED", filledQty=25,
                                averageTradedPrice=120.0))
    assert abs(ctx.closed_today[0].qty) == 25
    assert len(ctx.positions) == 1
    assert abs(ctx.positions[0].qty) == UNITS - 25
    assert ctx.positions[0].is_open


def test_a_clean_fill_leaves_the_ledger_untouched(db):
    ctx = _ctx()
    oid = _order_id(ctx)
    before = (ctx.positions[0].entry_price, ctx.positions[0].qty,
              ctx._fees_today)
    ctx.reconciler.apply(_alert(orderNo=oid, status="TRADED", filledQty=UNITS,
                                averageTradedPrice=145.0))
    assert (ctx.positions[0].entry_price, ctx.positions[0].qty,
            ctx._fees_today) == before


def test_a_correction_for_a_gone_position_does_not_raise(db):
    ctx = _ctx()
    oid = _order_id(ctx)
    ctx._open.clear()
    ctx.reconciler.apply(_alert(orderNo=oid, status="REJECTED"))   # no crash
