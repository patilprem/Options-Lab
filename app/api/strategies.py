"""
REST API — what your UI talks to.

POST   /strategies                     paste code -> validate -> register
GET    /strategies                     list all with state + capital
GET    /strategies/{id}                detail (code, meta, state)
PATCH  /strategies/{id}                {"name": "..."} -> rename
PUT    /strategies/{id}/code           {"code": "..."} -> validate -> replace (blocked while running/deployed)
DELETE /strategies/{id}                remove strategy + its history (blocked while running/deployed)
POST   /strategies/{id}/allocate       {"capital": 500000, "square_off_on_pause": false}
POST   /strategies/{id}/backtest       {"from_date": "...", "to_date": "...", "capital": ...}
GET    /strategies/{id}/backtests      past runs (summary + daily P&L)
POST   /strategies/{id}/deploy         load into paper engine (starts PAUSED)
POST   /strategies/{id}/play           allow entries
POST   /strategies/{id}/pause          block new entries (see pause semantics)
POST   /strategies/{id}/stop           square off + unload
GET    /strategies/{id}/performance    paper daily P&L + open positions
"""

from __future__ import annotations

import asyncio
from datetime import datetime, date, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.data.dhan_client import UNDERLYINGS

from app.core import loader, registry
from app.core.registry import State
from app.data.store import get_store
from app.engines import backtest as bt
from app.engines import walkforward as wf
from app.engines import live as L
from app.engines.paper import MarketHub, PaperRunner
from app.engines.live import LiveRunner
from app.engines import scanner
from app.engines.scanner import StockScanner

router = APIRouter(prefix="/strategies", tags=["strategies"])

# module-level singletons (fine for a single-user platform)
_store = get_store()
hub = MarketHub(_store)
runner = PaperRunner(hub)
live_runner = LiveRunner(hub)   # M8: LIVE ledgers only; paper is untouched
scanner_engine = StockScanner(_store)   # F2/F3 FNO stock scanner (shared store)
from app.engines import signals as _signals
_signals.register(scanner_engine)       # F6: ctx.signal() reads from the scanner
from app.engines.scanner_trader import ScannerTrader
scanner_engine.trader = ScannerTrader(_store)   # positional paper book on screener picks


class CreateReq(BaseModel):
    name: str
    code: str


class RenameReq(BaseModel):
    name: str


class UpdateCodeReq(BaseModel):
    code: str


class AllocateReq(BaseModel):
    capital: float
    square_off_on_pause: bool | None = None


class DeployReq(BaseModel):
    capital: float
    square_off_on_pause: bool = False
    start_immediately: bool = False


class ParamsReq(BaseModel):
    params: dict


class MonteCarloReq(BaseModel):
    n_sims: int = 500
    source: str = "PAPER"      # PAPER | BACKTEST


class BacktestReq(BaseModel):
    from_date: str          # "2025-01-01"
    to_date: str            # "2025-03-31"
    capital: float = 1_000_000.0


class WalkForwardReq(BaseModel):
    from_date: str
    to_date: str
    capital: float = 1_000_000.0
    folds: int = 4
    is_frac: float = 0.7                 # in-sample fraction of each fold
    param_grid: dict = {}                # {param_name: [values, ...]}
    metric: str = "sharpe"               # optimize on this in-sample metric
    max_runs: int = 200


def _instantiate(rec):
    cls = loader.load_strategy_class(rec.code)
    obj = cls()
    overrides = registry.get_params(rec.id)
    if overrides and hasattr(obj, "params") and isinstance(obj.params, dict):
        obj.params.update(overrides)
    return obj


def _rec_or_404(sid: str) -> registry.StrategyRecord:
    rec = registry.get(sid)
    if rec is None:
        raise HTTPException(404, f"strategy {sid} not found")
    return rec


@router.get("/{sid}/calendar")
def calendar(sid: str, mode: str = "PAPER"):
    """Daily P&L rows for calendar rendering. mode=PAPER|LIVE uses live
    forward-test ledgers; mode=BACKTEST uses the most recent backtest run."""
    _rec_or_404(sid)
    mode = mode.upper()
    if mode == "BACKTEST":
        runs = registry.backtests(sid)
        for run in runs:
            daily = run.get("result", {}).get("daily")
            if daily:
                return {"mode": mode, "source": run["id"],
                        "days": [{"date": d["date"],
                                  "pnl": round(d["realized"] + d["unrealized_eod"], 2),
                                  "trades": d.get("trades", 0)} for d in daily]}
        return {"mode": mode, "source": None, "days": []}
    rows = registry.performance_rows(sid, mode)
    return {"mode": mode, "source": "ledger",
            "days": [{"date": r["trade_date"],
                      "pnl": round(r["realized"] + r["unrealized"], 2),
                      "trades": None} for r in rows]}


@router.post("/{sid}/params")
def set_params(sid: str, req: ParamsReq):
    rec = _rec_or_404(sid)
    registry.set_params(sid, req.params)
    registry.record_event("info", "lifecycle", f"Params updated: {req.params}", sid)
    return {"id": sid, "params": registry.get_params(sid),
            "note": "applies to new backtests and the next deploy"}


@router.get("/{sid}/params")
def get_params(sid: str):
    rec = _rec_or_404(sid)
    base = rec.meta.get("params", {})
    return {"defaults": base, "overrides": registry.get_params(sid),
            "effective": {**base, **registry.get_params(sid)}}


@router.post("/{sid}/montecarlo")
def montecarlo(sid: str, req: MonteCarloReq):
    """Reshuffle observed daily P&L to estimate the drawdown distribution
    — answers 'how bad could an ordinary run of these same days look?'"""
    _rec_or_404(sid)
    cal = calendar(sid, req.source)
    pnls = [d["pnl"] for d in cal["days"]]
    if len(pnls) < 5:
        return {"ok": False, "message": f"Need at least 5 {req.source.lower()} days; have {len(pnls)}."}
    import random
    max_dds, finals = [], []
    for _ in range(min(req.n_sims, 5000)):
        seq = pnls[:]
        random.shuffle(seq)
        eq = peak = dd = 0.0
        for p in seq:
            eq += p
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        max_dds.append(dd)
        finals.append(eq)
    max_dds.sort()
    def pct(a, q): return round(a[min(len(a) - 1, int(q * len(a)))], 2)
    return {"ok": True, "source": req.source, "days": len(pnls), "sims": len(max_dds),
            "total_pnl": round(sum(pnls), 2),
            "max_drawdown": {"p50": pct(max_dds, .5), "p90": pct(max_dds, .9),
                             "p99": pct(max_dds, .99), "worst": round(max_dds[-1], 2)},
            "note": "Order-shuffle MC: same days, different order. It bounds "
                    "sequencing risk, not strategy risk."}


activity_router = APIRouter(tags=["activity"])


@activity_router.get("/activity")
def activity(date: str = ""):
    from datetime import datetime as _dt
    day = date or _dt.now().date().isoformat()
    return {"date": day, "events": registry.events_for(day)}


@activity_router.get("/feed/status")
def feed_status():
    """Market-data feed health for the dashboard's Feed pill."""
    return hub.feed_status()


data_router = APIRouter(tags=["data"])


@data_router.get("/data/coverage")
def data_coverage():
    """Backtestable date range per underlying, straight from the store."""
    out, store_kind = [], type(_store).__name__
    if store_kind == "DataStore":
        rows, opt = _store.coverage()   # locked — safe alongside a running backtest
        for u, lo, hi, n in rows:
            out.append({"underlying": u, "from": str(lo)[:10], "to": str(hi)[:10],
                        "underlying_bars": n, "option_bars": opt.get(u, 0)})
    rec_names = sorted(set(
        u.strip() for u in (registry.setting("record_underlyings", "NIFTY,BANKNIFTY")
                            + "," + registry.setting("mcx_underlyings", "CRUDEOIL,GOLD")
                            ).split(",") if u.strip()))
    return {"store": store_kind,
            "synthetic": store_kind == "SyntheticStore",
            "coverage": out,
            "recording_on": registry.setting("recording", "on") == "on",
            "recording_underlyings": rec_names,
            "recording_fields": ["ltp", "bid", "ask", "iv", "oi", "volume",
                                 "delta", "theta", "vega", "gamma", "spot",
                                 "spot 5-min bars"],
            "recording": _store.recording_status() if hasattr(_store, "recording_status") else [],
            "mcx_recording": registry.setting("mcx_recording", "on") == "on",
            "mcx_underlyings": registry.setting("mcx_underlyings", "CRUDEOIL,GOLD").split(","),
            "note": ("Synthetic store active — all dates 'available' but fake. "
                     "Backfill real data to see true coverage here."
                     if store_kind == "SyntheticStore" else "")}


_MATURITY_STAGES = [
    # (min learning days, stage, what it unlocks)
    (0,  "Infant",         "collecting first sessions — no conclusions yet"),
    (10, "Learning",       "directional reads OK; patterns are hypotheses only"),
    (25, "Adolescent",     "pattern scoreboard becomes meaningful (10+ samples)"),
    (40, "Research-ready", "chain-filter research (OI/IV) statistically testable"),
    (60, "Mature",         "full re-research + walk-forward on chain filters"),
]
_MATURITY_TARGET = 60      # sessions for full research readiness


@data_router.get("/data/maturity")
def data_maturity():
    """Health of the analysis machinery: how many sessions it has observed,
    which underlyings, how much experience the paper trader has accumulated,
    and which research stage that unlocks."""
    ls = (_store.learning_stats() if hasattr(_store, "learning_stats")
          else {"underlyings": [], "learning_days": 0, "chain_rows_total": 0})
    days = ls["learning_days"]
    stage, unlocks = _MATURITY_STAGES[0][1], _MATURITY_STAGES[0][2]
    for lo, s, u in _MATURITY_STAGES:
        if days >= lo:
            stage, unlocks = s, u
    nxt = next(((lo, s) for lo, s, _ in _MATURITY_STAGES if lo > days), None)
    paper_trades = registry.count_trades(mode="PAPER")
    return {
        "stage": stage,
        "unlocks": unlocks,
        "learning_days": days,
        "target_days": _MATURITY_TARGET,
        "maturity_pct": min(100, round(100 * days / _MATURITY_TARGET)),
        "next_stage": ({"name": nxt[1], "at_days": nxt[0],
                        "days_to_go": nxt[0] - days} if nxt else None),
        "underlyings": ls["underlyings"],
        "chain_rows_total": ls["chain_rows_total"],
        "paper_trades_observed": paper_trades,
        "hypotheses_tracked": 5,   # docs/observations.md pattern scoreboard
    }


@data_router.get("/data/footprint")
def data_footprint(underlying: str = "NIFTY", date: str | None = None):
    """One session's chain footprint (per-strike OI/IV shifts, PCR timeline,
    spot path, largest moves) from the live recording — the post-market
    analysis feed for pattern research."""
    if not hasattr(_store, "day_footprint"):
        raise HTTPException(400, "real DataStore required (synthetic active)")
    from datetime import datetime as _dt
    return _store.day_footprint(underlying, date or _dt.now().date().isoformat())


@router.post("/{sid}/wipe_day")
def wipe_day(sid: str, day: str):
    """Incident cleanup: erase one strategy's PAPER trades/P&L/state for one
    day (fills produced by bad quotes must not poison calibration data)."""
    _rec_or_404(sid)
    res = registry.wipe_paper_day(sid, day)
    registry.record_event("warn", "engine", f"paper day {day} wiped: {res}", sid)
    return res


@router.post("/{sid}/recompute_equity")
def recompute_equity(sid: str, mode: str = "PAPER", from_date: str | None = None):
    """Rebuild equity_eod for daily_pnl rows (mode) from from_date onward (the
    whole ledger if omitted), re-chaining each day's stored realized+unrealized
    forward. wipe_day/manual_trade now do this automatically for days after the
    one they touch; use this to repair drift left over from before that, or
    after any other manual edit to daily_pnl."""
    _rec_or_404(sid)
    n = registry.recompute_equity_chain(sid, mode, from_date)
    registry.record_event("info", "engine",
                          f"equity chain recomputed ({mode}) from "
                          f"{from_date or 'day 1'}: {n} row(s)", sid)
    return {"rows_updated": n}


class ManualLegReq(BaseModel):
    option_type: str            # CE | PE
    action: str                 # entry side: BUY | SELL
    strike: float
    qty: int                    # units (lots x lot size), > 0
    entry_price: float
    exit_price: float
    entry_ts: str               # "09:47" or "09:47:23" (IST)
    exit_ts: str
    exit_reason: str = "manual"
    margin: float = 0.0         # blocked at entry, if you want it on the blotter
    tag: str = ""


class ManualTradeReq(BaseModel):
    day: str                    # YYYY-MM-DD
    underlying: str = "NIFTY"
    expiry: str                 # YYYY-MM-DD
    legs: list[ManualLegReq]
    update_daily: bool = True   # False = blotter rows only, leave daily_pnl alone


@router.post("/{sid}/manual_trade")
def manual_trade(sid: str, req: ManualTradeReq):
    """Book a completed round-trip at ACTUAL prices (PAPER ledger). Companion
    to wipe_day: after erasing a day filled off frozen/bad quotes, re-enter
    the day's trade with the real fills. Charges come from the shared cost
    model (engines/fills.py) so the rows stay comparable with engine fills;
    realized is net of fees, same as Position.realized_pnl."""
    from datetime import time as _time
    from app.core.contract import Action as _Action
    from app.engines import fills as F

    rec = _rec_or_404(sid)
    if not req.legs:
        raise HTTPException(422, "at least one leg required")
    try:
        day = date.fromisoformat(req.day)
        expiry = date.fromisoformat(req.expiry)
    except ValueError as e:
        raise HTTPException(422, f"bad date: {e}")

    def _ts(hhmm: str) -> str:
        try:
            return datetime.combine(day, _time.fromisoformat(hhmm)) \
                .isoformat(sep=" ", timespec="seconds")
        except ValueError as e:
            raise HTTPException(422, f"bad time '{hhmm}': {e}")

    fee_cfg = F.FeeConfig()
    realized = fees_total = 0.0
    booked = []
    for leg in req.legs:
        side, opt = leg.action.upper(), leg.option_type.upper()
        if side not in ("BUY", "SELL"):
            raise HTTPException(422, "leg.action must be BUY or SELL")
        if opt not in ("CE", "PE"):
            raise HTTPException(422, "leg.option_type must be CE or PE")
        if leg.qty <= 0 or leg.entry_price <= 0 or leg.exit_price < 0:
            raise HTTPException(422, "qty and prices must be positive")
        exit_side = "SELL" if side == "BUY" else "BUY"
        e_fees = F.charges(leg.entry_price * leg.qty, _Action[side], fee_cfg)
        x_fees = F.charges(leg.exit_price * leg.qty, _Action[exit_side], fee_cfg)
        contract = f"{req.underlying} {expiry:%d%b%y} {leg.strike:g} {opt}".upper()
        registry.record_trade(sid, "PAPER", {
            "ts": _ts(leg.entry_ts), "contract": contract, "side": side,
            "qty": leg.qty, "price": round(leg.entry_price, 2),
            "fees": round(e_fees, 2), "margin": round(leg.margin, 2),
            "reason": "entry", "tag": leg.tag or "manual"})
        registry.record_trade(sid, "PAPER", {
            "ts": _ts(leg.exit_ts), "contract": contract, "side": exit_side,
            "qty": leg.qty, "price": round(leg.exit_price, 2),
            "fees": round(x_fees, 2), "margin": 0.0,
            "reason": leg.exit_reason, "tag": leg.tag or "manual"})
        signed = leg.qty if side == "BUY" else -leg.qty
        leg_pnl = (leg.exit_price - leg.entry_price) * signed - (e_fees + x_fees)
        realized += leg_pnl
        fees_total += e_fees + x_fees
        booked.append({"contract": contract, "side": side, "qty": leg.qty,
                       "entry": leg.entry_price, "exit": leg.exit_price,
                       "fees": round(e_fees + x_fees, 2), "pnl": round(leg_pnl, 2)})

    daily = None
    if req.update_daily:
        prev = [r for r in registry.paper_performance(sid)
                if r["trade_date"] < req.day]
        base = prev[-1]["equity_eod"] if prev else rec.allocated_capital
        daily = {"trade_date": req.day, "realized": round(realized, 2),
                 "unrealized": 0.0, "fees": round(fees_total, 2),
                 "equity_eod": round(base + realized, 2)}
        registry.save_paper_day(sid, req.day, daily["realized"], 0.0,
                                daily["fees"], daily["equity_eod"])
        # any day after req.day was chained off its OLD equity_eod — refresh
        # the curve forward so a wipe_day+manual_trade correction doesn't
        # leave later days stale.
        registry.recompute_equity_chain(sid, "PAPER", req.day)
    registry.record_event("info", "fill",
                          f"manual booking {req.day}: {len(booked)} leg(s), "
                          f"P&L {realized:+.2f} (fees {fees_total:.2f})", sid)
    return {"booked": booked, "realized": round(realized, 2),
            "fees": round(fees_total, 2), "daily_row": daily}


@data_router.post("/data/purge_offhours")
def purge_offhours():
    """Remove recorded rows stamped outside exchange sessions (weekend junk
    from the pre-gate recorder) + phantom weekend P&L rows. Idempotent."""
    if not hasattr(_store, "purge_offhours"):
        raise HTTPException(400, "real DataStore required")
    res = _store.purge_offhours()
    res["daily_pnl_phantom_rows"] = registry.purge_phantom_days()
    registry.record_event("info", "data", f"off-hours purge: {res}")
    return res


@data_router.post("/data/expiries/rebuild")
def rebuild_expiries(underlying: str = "NIFTY"):
    """Derive the weekly-expiry calendar from the stored option data (ATM-
    straddle expiry-day collapse) and fill the NULL expiry column that the
    rolling expired-options API can't provide. Idempotent; also runs
    automatically after every backfill."""
    if not hasattr(_store, "_q"):
        raise HTTPException(400, "real DataStore required (synthetic active)")
    from app.data import expiries
    res = expiries.rebuild(_store, underlying)
    registry.record_event("info", "data",
                          f"expiry calendar rebuilt: {res}")
    return res


@data_router.post("/data/recording/{state}")
def set_recording(state: str):
    on = "on" if state == "on" else "off"
    registry.set_setting("recording", on)
    registry.set_setting("mcx_recording", on)     # legacy toggle follows
    registry.record_event("info", "feed", f"live market recording turned {on}")
    return {"recording": on == "on"}


# --- backfill (pick a period or MAX) ---------------------------------------

# Dhan serves up to ~5 years of intraday + expired-options history.
_PERIODS = {"3m": 90, "6m": 182, "1y": 365, "2y": 730, "5y": 1825, "max": 1825}


class BackfillReq(BaseModel):
    underlying: str = "NIFTY"
    period: str = "2y"                     # default 2y; 3m|6m|1y|2y|5y|max, or from/to
    from_date: str | None = None
    to_date: str | None = None
    strike_offsets: int = 2                # ATM +/- N
    interval: int = 5


def _period_dates(req: BackfillReq) -> tuple[date, date]:
    end = date.today()
    if req.from_date and req.to_date:
        return date.fromisoformat(req.from_date), date.fromisoformat(req.to_date)
    days = _PERIODS.get(req.period, _PERIODS["max"])
    return end - timedelta(days=days), end


def _chunks(days: int, size: int) -> int:
    return max(1, -(-days // size))        # ceil


async def _run_backfill(req: BackfillReq, start: date, end: date):
    from app.data import backfill_job
    # Reuse the server's DuckDB via an independent cursor (no second file lock);
    # backfill_job persists live progress to registry.backfill_status.
    view = type("V", (), {"con": _store.con.cursor()})()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: backfill_job.run(
        req.underlying, start, end, strike_offsets=req.strike_offsets,
        interval=req.interval, store=view))


@data_router.post("/data/backfill")
async def start_backfill(req: BackfillReq):
    if registry.get_backfill_status().get("running"):
        raise HTTPException(409, "a backfill is already running")
    if req.underlying not in UNDERLYINGS:
        raise HTTPException(400, f"unknown underlying {req.underlying}; "
                                 f"known: {', '.join(UNDERLYINGS)}")
    if not hasattr(_store, "con"):
        raise HTTPException(400, "backfill needs the real DuckDB store; it is "
                                 "unavailable (synthetic mode / file locked).")
    start, end = _period_dates(req)
    if start >= end:
        raise HTTPException(400, "from_date must be before to_date")
    days = (end - start).days + 1
    offs = range(-req.strike_offsets, req.strike_offsets + 1)
    est = _chunks(days, 89) + len(offs) * 2 * _chunks(days, 29)
    asyncio.create_task(_run_backfill(req, start, end))
    return {"started": True, "underlying": req.underlying,
            "from": start.isoformat(), "to": end.isoformat(),
            "estimated_chunks": est,
            "note": "Expired-options history is slow (~1 min/chunk). Poll "
                    "/data/backfill/status for progress."}


@data_router.get("/data/backfill/status")
def backfill_status():
    import time as _t
    st = registry.get_backfill_status()
    # Heartbeat: if a "running" job hasn't updated in >3 min it died (shutdown /
    # crash). Show it as interrupted so the UI is honest and re-runs can resume.
    if st.get("running") and st.get("updated_at") and _t.time() - st["updated_at"] > 180:
        st = {**st, "running": False, "message": "interrupted — re-run to resume"}
        registry.set_backfill_status(st)
    pct = round(100 * st.get("done", 0) / st["total"], 1) if st.get("total") else 0
    return {**st, "pct": pct, "periods": list(_PERIODS)}


trades_router = APIRouter(tags=["trades"])


@trades_router.get("/trades")
def trade_history(from_date: str = "", to_date: str = "",
                  strategy_id: str = "", mode: str = "", fmt: str = "json"):
    rows = registry.all_trades(from_date, to_date, strategy_id, mode)
    if fmt == "csv":
        import io, csv
        from fastapi.responses import PlainTextResponse
        buf = io.StringIO()
        cols = ["ts", "strategy", "mode", "contract", "side", "qty",
                "price", "fees", "margin", "reason", "tag"]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        return PlainTextResponse(buf.getvalue(), media_type="text/csv")
    return {"count": len(rows), "trades": rows}


portfolio_router = APIRouter(tags=["portfolio"])


def _ist_today() -> str:
    """IST calendar date — the single source of truth for 'what day is it' on
    every P&L read. The whole paper engine (PaperContext._roll_day,
    _session_date()) keys off IST wall-clock; a naive datetime.now() call
    disagrees with that near the IST/UTC midnight offset on any server not
    clocked to IST, which can misroute the reset logic below."""
    from app.engines.paper import IST
    return datetime.now(IST).date().isoformat()


def _today_day_pnl(rec, ctx, today: str) -> tuple[float, int, list[dict]]:
    """The single reset-aware read of ONE strategy's today's P&L. Shared by
    /portfolio/today and /{sid}/performance so they can't drift into two
    different (and differently buggy) answers to the same question — that
    drift is exactly how a fix in one endpoint left the other one showing
    stale P&L under today's date.

    Returns (day_pnl, n_open, trades_today_rows).

    ctx.day_pnl rolls on the FIRST BAR of a new trading day (PaperContext.
    _roll_day, called from push_bar) — so before that first bar arrives
    (anywhere from market close through the next session's open, including
    all of a weekend), ctx.day_pnl still holds the PREVIOUS session's total.
    Trust it only once the engine's own clock (ctx.now, driven by the same
    bar) is actually on today. Otherwise fall back to a persisted row for
    today if one exists (a strategy that traded today then got stopped), else
    zero — and as a final backstop, zero-activity (no fill today, nothing
    open right now) can never legitimately produce a nonzero P&L, so that
    combination always wins over a possibly-stale persisted row."""
    n_open = len(ctx.positions) if ctx else 0
    t_rows = registry.trades_for(rec.id, today, "PAPER")
    today_row = next((r for r in registry.performance_rows(rec.id, "PAPER")
                      if r["trade_date"] == today), None)
    if ctx and ctx.now.date().isoformat() == today:
        day_pnl = round(ctx.day_pnl, 2)
    elif today_row:
        day_pnl = round((today_row["realized"] or 0.0) + (today_row["unrealized"] or 0.0), 2)
    else:
        day_pnl = 0.0
    if not t_rows and n_open == 0:
        day_pnl = 0.0
    return day_pnl, n_open, t_rows


@portfolio_router.get("/portfolio/today")
def portfolio_today():
    """Combined view across every deployed strategy: today's P&L / ROI,
    all open positions, all trades — plus a per-strategy breakdown."""
    today = _ist_today()
    strategies_out, positions, trades = [], [], []
    total_alloc = total_day = total_margin = 0.0
    for rec in registry.list_all():
        deployed = rec.state in (State.RUNNING, State.DEPLOYED_PAUSED)
        ctx = runner.contexts.get(rec.id)
        day_pnl, n_open, t_rows = _today_day_pnl(rec, ctx, today)
        # A strategy that traded today still belongs in today's P&L even if it
        # has since been stopped — otherwise stopping it silently erases the
        # day. Gated on t_rows/n_open (reliable, freshly computed), never on a
        # persisted row alone — a stale row must not keep a dead strategy
        # visible under today's date either.
        if not deployed and ctx is None and not t_rows and n_open == 0:
            continue
        open_rows = []
        if ctx:
            for p in ctx.positions:
                open_rows.append({
                    "strategy": rec.name, "strategy_id": rec.id,
                    "tag": p.tag, "type": p.leg.option_type.value,
                    "strike": p.strike, "expiry": str(p.expiry), "qty": p.qty,
                    "entry": p.entry_price, "mtm": p.mtm_price,
                    "stop_loss": p.stop_loss, "target": p.target,
                    "margin": p.margin_blocked,
                    "unrealized": round(p.unrealized_pnl, 2),
                })
                total_margin += p.margin_blocked or 0.0
        positions.extend(open_rows)
        for t in t_rows:
            trades.append({**t, "strategy": rec.name, "strategy_id": rec.id})
        cap = rec.allocated_capital or 0.0
        total_alloc += cap
        total_day += day_pnl
        strategies_out.append({
            "id": rec.id, "name": rec.name, "state": rec.state.value,
            "allocated_capital": cap, "day_pnl": day_pnl,
            "day_roi_pct": round(100 * day_pnl / cap, 2) if cap > 0 else 0.0,
            "open_positions": len(open_rows), "trades_today": len(t_rows),
        })
    trades.sort(key=lambda t: t.get("ts", ""))
    # Portfolio equity: EVERY strategy's allocation + its lifetime realized
    # P&L (daily rows carry today's realized too once written), plus live
    # unrealized on open positions. Spans all strategies incl. stopped ones
    # so past losses never vanish from the equity card.
    equity = 0.0
    alloc_all = 0.0
    for rec in registry.list_all():
        cap_i = rec.allocated_capital or 0.0
        if cap_i <= 0:
            continue
        alloc_all += cap_i
        equity += cap_i + registry.cum_pnl(rec.id, "PAPER")
    equity += sum(p["unrealized"] for p in positions)
    trades_sorted = trades
    return {
        "date": today,
        "mode": "PAPER",
        "totals": {
            "allocated_capital": round(total_alloc, 2),
            "allocated_capital_all": round(alloc_all, 2),
            "equity": round(equity, 2),
            "growth": round(equity - alloc_all, 2),
            "day_pnl": round(total_day, 2),
            "day_roi_pct": round(100 * total_day / total_alloc, 2) if total_alloc > 0 else 0.0,
            "margin_used": round(total_margin, 2),
            "day_roi_on_margin_pct": (round(100 * total_day / total_margin, 2)
                                      if total_margin > 0 else 0.0),
            "open_positions": len(positions),
            "trades": len(trades),
        },
        "live": {"enabled": False,
                 "note": "Live trading is not wired yet. When it is, live "
                         "P&L will appear in its own section — never mixed "
                         "with paper numbers."},
        "strategies": strategies_out,
        "open_positions": positions,
        "trades_today": trades,
    }


# --- risk panel (M7) --------------------------------------------------------

risk_router = APIRouter(tags=["risk"])


class RiskSettingsReq(BaseModel):
    max_daily_loss: float | None = None      # portfolio-wide (₹; 0 = disabled)
    default_loss_cap: float | None = None    # per-strategy default (₹)
    per_strategy: dict = {}                   # {sid: cap}


@risk_router.get("/risk")
def risk_view():
    """Portfolio risk snapshot: margin utilization, day P&L vs max-loss,
    exposure by underlying/expiry, per-strategy caps."""
    return runner.risk_snapshot()


@risk_router.post("/risk/settings")
def set_risk(req: RiskSettingsReq):
    if req.max_daily_loss is not None:
        registry.set_setting("risk_max_daily_loss", str(max(0.0, req.max_daily_loss)))
    if req.default_loss_cap is not None:
        registry.set_setting("risk_default_loss_cap", str(max(0.0, req.default_loss_cap)))
    for sid, cap in (req.per_strategy or {}).items():
        registry.set_setting(f"risk_loss_cap:{sid}", str(max(0.0, float(cap))))
    registry.record_event("info", "risk", "Risk limits updated")
    runner.enforce_risk()  # apply immediately against current P&L
    return runner.risk_snapshot()


# --- diagnostics -----------------------------------------------------------

diag_router = APIRouter(tags=["diag"])


@diag_router.get("/diag/index_vol")
def index_vol_status():
    """Last verdict of the index-futures volume self-check + its mode. The
    check runs itself on the VPS during market hours (MarketHub loop) and pushes
    the verdict to ntfy; this surfaces it for the dashboard too."""
    return {
        "result": registry.setting("index_vol_check_result", "(not run yet)"),
        "mode": registry.setting("index_vol_check", "auto"),
        "last_attempt_day": registry.setting("index_vol_check_ran", ""),
        "companion_enabled": registry.setting("index_futures_volume", "off") == "on",
    }


@diag_router.post("/diag/index_vol/run")
def index_vol_force():
    """Force the self-check to run at the next in-session opportunity (clears
    the once-per-day guard). Verdict arrives via ntfy + GET /diag/index_vol."""
    registry.set_setting("index_vol_check", "force")
    registry.set_setting("index_vol_check_ran", "")
    registry.record_event("info", "diag", "index-vol self-check armed (force)")
    return {"status": "armed", "note": "runs at the next live NSE session"}


# --- live execution (M8) — gated -------------------------------------------

live_router = APIRouter(tags=["live"])


class LiveSettingsReq(BaseModel):
    enabled: bool | None = None
    dry_run: bool | None = None
    max_lots: int | None = None


class KillReq(BaseModel):
    action: str = "arm"        # arm | disarm


@live_router.get("/live/status")
def live_status():
    return {
        "enabled": L.live_enabled(),
        "dry_run": L.dry_run(),
        "max_lots": L.max_lots(),
        "kill_armed": registry.setting("live_kill_armed", "no") == "yes",
        "deployed": list(live_runner.contexts),
        "note": ("DRY-RUN: orders are logged, not sent. Real orders also require "
                 "running on the whitelisted static IP." if L.dry_run()
                 else "REAL ORDERS ENABLED — orders will be sent to Dhan."),
    }


@live_router.post("/live/settings")
def live_settings(req: LiveSettingsReq):
    if req.enabled is not None:
        registry.set_setting("live_enabled", "on" if req.enabled else "off")
    if req.dry_run is not None:
        registry.set_setting("live_dry_run", "off" if req.dry_run is False else "on")
    if req.max_lots is not None:
        registry.set_setting("live_max_lots", str(max(1, req.max_lots)))
    registry.record_event("warn", "live",
                          f"Live settings: enabled={L.live_enabled()} "
                          f"dry_run={L.dry_run()} max_lots={L.max_lots()}")
    return live_status()


@live_router.post("/live/kill")
def live_kill(req: KillReq):
    ks = L.KillSwitch(L.make_order_client())
    resp = ks.arm() if req.action == "arm" else ks.disarm()
    return {"action": req.action, "armed": ks.armed(), "broker_response": resp}


@live_router.post("/strategies/{sid}/live/ack")
def live_ack(sid: str):
    _rec_or_404(sid)
    registry.set_setting(f"live_checklist_ack:{sid}", "yes")
    registry.record_event("info", "live", "live checklist acknowledged", sid)
    return {"id": sid, "acknowledged": True}


@live_router.post("/strategies/{sid}/deploy_live")
async def deploy_live(sid: str):
    rec = _rec_or_404(sid)
    if not L.live_enabled():
        raise HTTPException(400, "Live trading is disabled. Enable it in Live settings first.")
    if not L.checklist_ack(sid):
        raise HTTPException(400, "Acknowledge the live checklist before deploying live.")
    if rec.allocated_capital <= 0:
        raise HTTPException(400, "Allocate capital before deploying.")
    if rec.state in (State.VALIDATED, State.STOPPED):
        registry.transition(sid, State.DEPLOYED_PAUSED)
    await live_runner.deploy(registry.get(sid), _instantiate(rec))
    return {"id": sid, "mode": "LIVE", "dry_run": L.dry_run(),
            "state": "DEPLOYED_PAUSED", "hint": "call /strategies/{id}/live/play to arm entries"}


@live_router.post("/strategies/{sid}/live/{cmd}")
async def live_control(sid: str, cmd: str):
    ctx = live_runner.contexts.get(sid)
    if ctx is None:
        raise HTTPException(404, "strategy not deployed live")
    if cmd == "play":
        registry.transition(sid, State.RUNNING); ctx.set_paused(False)
    elif cmd == "pause":
        registry.transition(sid, State.DEPLOYED_PAUSED); ctx.set_paused(True)
    elif cmd == "stop":
        await live_runner.stop(sid); registry.transition(sid, State.STOPPED)
    else:
        raise HTTPException(400, "cmd must be play|pause|stop")
    return {"id": sid, "cmd": cmd}


@router.post("")
def create_strategy(req: CreateReq):
    result = loader.validate(req.code)
    if not result.ok:
        raise HTTPException(422, detail={"errors": result.errors,
                                         "warnings": result.warnings})
    rec = registry.create(req.name, req.code)
    registry.set_meta(rec.id, {
        "underlying": result.meta.underlying,
        "segment": result.meta.segment,
        "timeframe": result.meta.timeframe,
        "params": result.meta.params,
        "class_name": result.strategy_class_name,
        "description": result.meta.description,
    })
    registry.transition(rec.id, State.VALIDATED)
    return {"id": rec.id, "state": "VALIDATED",
            "meta": registry.get(rec.id).meta, "warnings": result.warnings}


@router.get("")
def list_strategies():
    return [{"id": r.id, "name": r.name, "state": r.state.value,
             "allocated_capital": r.allocated_capital, "meta": r.meta}
            for r in registry.list_all()]


@router.get("/{sid}")
def get_strategy(sid: str):
    r = _rec_or_404(sid)
    return {"id": r.id, "name": r.name, "state": r.state.value,
            "allocated_capital": r.allocated_capital,
            "square_off_on_pause": r.square_off_on_pause,
            "meta": r.meta, "code": r.code}


@router.patch("/{sid}")
def rename_strategy(sid: str, req: RenameReq):
    _rec_or_404(sid)
    try:
        rec = registry.rename(sid, req.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    registry.record_event("info", "lifecycle", f"Renamed to '{rec.name}'", sid)
    return {"id": sid, "name": rec.name}


@router.put("/{sid}/code")
def update_code(sid: str, req: UpdateCodeReq):
    rec = _rec_or_404(sid)
    if rec.state in (State.RUNNING, State.DEPLOYED_PAUSED):
        raise HTTPException(400, "Stop the strategy before editing its code")
    result = loader.validate(req.code)
    if not result.ok:
        raise HTTPException(422, detail={"errors": result.errors,
                                         "warnings": result.warnings})
    registry.update_code(sid, req.code)
    registry.set_meta(sid, {
        "underlying": result.meta.underlying,
        "segment": result.meta.segment,
        "timeframe": result.meta.timeframe,
        "params": result.meta.params,
        "class_name": result.strategy_class_name,
        "description": result.meta.description,
    })
    registry.transition(sid, State.VALIDATED)
    registry.record_event("info", "lifecycle", "Code updated & revalidated", sid)
    return {"id": sid, "state": "VALIDATED",
            "meta": registry.get(sid).meta, "warnings": result.warnings}


@router.delete("/{sid}")
def delete_strategy(sid: str):
    rec = _rec_or_404(sid)
    if rec.state in (State.RUNNING, State.DEPLOYED_PAUSED):
        raise HTTPException(400, "Stop the strategy before deleting it")
    registry.delete(sid)
    return {"id": sid, "deleted": True}


@router.post("/{sid}/allocate")
def allocate(sid: str, req: AllocateReq):
    _rec_or_404(sid)
    rec = registry.allocate(sid, req.capital, req.square_off_on_pause)
    return {"id": sid, "allocated_capital": rec.allocated_capital,
            "square_off_on_pause": rec.square_off_on_pause}


@router.post("/{sid}/backtest")
def run_backtest(sid: str, req: BacktestReq):
    rec = _rec_or_404(sid)
    underlying = rec.meta.get("underlying", "")
    start = datetime.fromisoformat(req.from_date + " 09:15:00")
    end = datetime.fromisoformat(req.to_date + " 15:30:00")
    if not _store.has_data(underlying, start, end):
        return {
            "status": "data_unavailable",
            "message": (f"No historical data for {underlying} between "
                        f"{req.from_date} and {req.to_date}. Backtesting is "
                        "unavailable for this strategy right now — you can "
                        "still deploy it for paper trading, and its live "
                        "performance will build up day by day."),
            "can_paper_trade": True,
        }
    strat = _instantiate(rec)
    result = bt.run_backtest(strat, _store, start, end, req.capital)
    result["status"] = "ok"
    run_id = f"bt-{datetime.now():%Y%m%d%H%M%S}"
    registry.save_backtest(sid, run_id, req.from_date, req.to_date, result)
    return {"run_id": run_id, **result}


@router.post("/{sid}/walkforward")
def walkforward(sid: str, req: WalkForwardReq):
    rec = _rec_or_404(sid)
    underlying = rec.meta.get("underlying", "")
    start = datetime.fromisoformat(req.from_date + " 09:15:00")
    end = datetime.fromisoformat(req.to_date + " 15:30:00")
    if not _store.has_data(underlying, start, end):
        return {"status": "data_unavailable",
                "message": (f"No historical data for {underlying} between "
                            f"{req.from_date} and {req.to_date}.")}

    def factory(params: dict):
        obj = _instantiate(rec)
        if params and hasattr(obj, "params") and isinstance(obj.params, dict):
            obj.params.update(params)
        return obj

    # Stream progress to the events log, but don't flood it — log fold
    # boundaries and roughly every 10th run.
    def on_progress(done: int, total: int, msg: str):
        if done == total or "OOS" in msg or done % 10 == 0:
            registry.record_event("info", "engine",
                                  f"walk-forward {done}/{total}: {msg}", sid)

    registry.record_event("info", "engine",
                          f"walk-forward started: {req.folds} folds, "
                          f"grid={req.param_grid or 'none'}", sid)
    try:
        result = wf.run_walkforward(
            factory, _store, underlying, start, end, folds=req.folds,
            is_frac=req.is_frac, param_grid=req.param_grid, capital=req.capital,
            metric=req.metric, max_runs=req.max_runs, on_progress=on_progress)
    except ValueError as e:
        raise HTTPException(400, str(e))
    registry.record_event("info", "engine",
                          f"walk-forward done: OOS return "
                          f"{result.get('aggregate_oos', {}).get('return_pct', 0)}%", sid)
    return result


@router.get("/{sid}/backtests")
def list_backtests(sid: str):
    _rec_or_404(sid)
    return registry.backtests(sid)


@router.post("/{sid}/deploy")
async def deploy(sid: str, req: DeployReq | None = None):
    rec = _rec_or_404(sid)
    if req is not None:
        registry.allocate(sid, req.capital, req.square_off_on_pause)
        rec = registry.get(sid)
    if rec.allocated_capital <= 0:
        raise HTTPException(400, "allocate capital before deploying")
    registry.transition(sid, State.DEPLOYED_PAUSED)
    await runner.deploy(registry.get(sid), _instantiate(rec))
    registry.record_event("info", "lifecycle", f"Deployed for paper trading", sid)
    if req is not None and req.start_immediately:
        registry.transition(sid, State.RUNNING)
        runner.play(sid)
        return {"id": sid, "state": "RUNNING"}
    return {"id": sid, "state": "DEPLOYED_PAUSED",
            "hint": "call /play to start taking entries"}


@router.post("/{sid}/play")
def play(sid: str):
    _rec_or_404(sid)
    registry.transition(sid, State.RUNNING)
    runner.play(sid)
    return {"id": sid, "state": "RUNNING"}


@router.post("/{sid}/pause")
def pause(sid: str):
    rec = _rec_or_404(sid)
    registry.transition(sid, State.DEPLOYED_PAUSED)
    runner.pause(sid)
    return {"id": sid, "state": "DEPLOYED_PAUSED",
            "square_off_on_pause": rec.square_off_on_pause}


@router.post("/{sid}/stop")
async def stop(sid: str):
    _rec_or_404(sid)
    await runner.stop(sid)
    registry.transition(sid, State.STOPPED)
    return {"id": sid, "state": "STOPPED"}


@router.get("/{sid}/metrics")
def metrics(sid: str, mode: str = "PAPER"):
    """Capital growth + performance stats. mode=PAPER (default) or LIVE —
    the two are NEVER mixed; real money and simulated money stay separate."""
    rec = _rec_or_404(sid)
    rows = registry.performance_rows(sid, mode.upper())
    cap = rec.allocated_capital or 0.0
    if not rows or cap <= 0:
        return {"id": sid, "mode": mode.upper(), "has_data": False,
                "allocated_capital": cap}
    import math
    eq = [cap] + [r["equity_eod"] for r in rows]
    rets = [(eq[i] - eq[i-1]) / eq[i-1] for i in range(1, len(eq)) if eq[i-1] > 0]
    peak, max_dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak if peak else 0)
    pnl_days = [r["realized"] + r["unrealized"] for r in rows]
    win_days = sum(1 for p in pnl_days if p > 0)
    sharpe = 0.0
    if len(rets) > 1:
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
        sharpe = (mu / sd) * math.sqrt(252) if sd > 0 else 0.0
    growth = eq[-1] - cap
    return {
        "id": sid, "mode": mode.upper(), "has_data": True,
        "allocated_capital": cap,
        "current_equity": round(eq[-1], 2),
        "capital_growth": round(growth, 2),
        "return_pct": round(100 * growth / cap, 2),
        "max_drawdown_pct": round(100 * max_dd, 2),
        "sharpe": round(sharpe, 2),
        "trading_days": len(rows),
        "win_days_pct": round(100 * win_days / len(rows), 1),
        "avg_day_pnl": round(sum(pnl_days) / len(rows), 2),
        "best_day": round(max(pnl_days), 2),
        "worst_day": round(min(pnl_days), 2),
        "total_fees": round(sum(r["fees"] for r in rows), 2),
        "equity_curve": [{"date": r["trade_date"], "equity": r["equity_eod"]}
                         for r in rows],
    }


@router.get("/{sid}/performance")
def performance(sid: str):
    rec = _rec_or_404(sid)
    ctx = runner.contexts.get(sid)
    today = _ist_today()
    open_positions = []
    # day_pnl stays None (not 0.0) when there's no live context — the frontend
    # (PaperPanel.jsx) reads day_pnl===null as "not currently deployed" and
    # hides the live stat cards on that signal, so this distinction is load
    # bearing, not cosmetic. When ctx DOES exist, route through the same
    # reset-aware helper /portfolio/today uses — ctx.day_pnl alone rolls only
    # on the first bar of a new day, so reading it directly (as this endpoint
    # used to) showed the PREVIOUS session's total under today's date for the
    # whole overnight window, including the first few minutes after 09:15.
    day_pnl = None
    if ctx:
        day_pnl, _n_open, _t_rows = _today_day_pnl(rec, ctx, today)
        open_positions = [{
            "id": p.id, "tag": p.tag, "type": p.leg.option_type.value,
            "strike": p.strike, "expiry": str(p.expiry), "qty": p.qty,
            "entry": p.entry_price, "mtm": p.mtm_price,
            "stop_loss": p.stop_loss, "target": p.target,
            "margin": p.margin_blocked,
            "unrealized": round(p.unrealized_pnl, 2),
        } for p in ctx.positions]
    cap = rec.allocated_capital or 0.0
    day_roi = round(100 * day_pnl / cap, 2) if (day_pnl is not None and cap > 0) else None
    margin_used = round(ctx._margin_used, 2) if ctx else 0.0
    roi_margin = (round(100 * day_pnl / margin_used, 2)
                  if (day_pnl is not None and margin_used > 0) else None)
    return {"id": sid, "mode": "PAPER",
            "day_pnl": day_pnl, "day_roi_pct": day_roi,
            "margin_used": margin_used, "day_roi_on_margin_pct": roi_margin,
            "allocated_capital": cap,
            "open_positions": open_positions,
            "trades_today": registry.trades_for(sid, today, "PAPER"),
            "daily": registry.performance_rows(sid, "PAPER")}


# --- FNO stock scanner (F4) -------------------------------------------------

scanner_router = APIRouter(tags=["scanner"])


class ScannerSettingsReq(BaseModel):
    enabled: bool | None = None          # master on/off (needs live creds)
    alert_score: float | None = None     # ntfy alert threshold (0-100)
    record_chains: bool | None = None    # persist full chains for every
                                          # shortlisted name, not just held ones


@scanner_router.get("/scanner")
def scanner_view():
    """Ranked setup table: every scanned FNO stock with its composite score,
    bias, and the shortlist that got a Tier-2 chain deep-dive."""
    return {
        "enabled": registry.setting("scanner", "off") == "on",
        "alert_score": float(registry.setting("scanner_alert_score", "70")),
        "record_chains": registry.setting("scanner_record_chains", "off") == "on",
        "session": scanner._session_open(),
        "universe_size": len(scanner_engine._universe),
        "last_sweep": (str(scanner_engine._last_sweep_ts)
                       if scanner_engine._last_sweep_ts else None),
        "shortlist": scanner_engine.shortlist,
        "scores": scanner_engine.ranked_scores(),
    }


@scanner_router.get("/scanner/index-bias")
def scanner_index_bias():
    """Current NIFTY/BANKNIFTY constituent-weighted bias + recent history and
    the recorded accuracy (hit-rate vs realized move) so the signal is judged,
    not trusted."""
    out = {}
    for index in scanner.INDEX_CONSTITUENTS:
        out[index] = {
            "current": scanner_engine.index_bias.get(index),
            "history": (_store.recent_index_bias(index)
                        if hasattr(_store, "recent_index_bias") else []),
            "accuracy": (_store.index_bias_accuracy(index)
                         if hasattr(_store, "index_bias_accuracy") else []),
        }
    return out


@scanner_router.get("/scanner/validation")
def scanner_validation(days: int = 21, horizon_min: int = 30):
    """Measured forward-return hit-rate of flagged setups over the last `days`,
    bucketed by score band — the evidence the scanner must earn BEFORE any
    signal is traded. Empty until enough sessions are recorded."""
    from datetime import date as _date, timedelta as _td
    since = _date.today() - _td(days=max(1, days))
    return scanner_engine.validate(since, horizon_min)


@scanner_router.post("/scanner/backfill/{symbol}")
def scanner_backfill(symbol: str, days: int = 30):
    """On-demand expired-options backfill for ONE flagged stock (full-universe
    options backfill is prohibitively slow — F6 backfills only flagged names).
    Kicks a background thread; needs live creds + a real store."""
    symbol = symbol.upper()
    if not hasattr(_store, "con"):
        raise HTTPException(400, "backfill needs a real DataStore")

    import threading
    from datetime import date as _date, timedelta as _td

    def _job():
        try:
            from app.data import dhan_client
            uni = dhan_client.resolve_fno_universe(store=_store)
            u = uni.get(symbol)
            if not u or u.get("spot_security_id") is None:
                registry.record_event("warn", "scanner",
                                      f"backfill: {symbol} not in FNO universe")
                return
            dhan_client.UNDERLYINGS[symbol] = {
                "security_id": int(u["spot_security_id"]), "segment": "NSE_EQ",
                "fno_segment": u.get("fno_segment", "NSE_FNO"), "instrument": "OPTSTK"}
            end = _date.today()
            start = end - _td(days=max(1, days))
            view = type("V", (), {"con": _store.con.cursor()})()
            dhan_client.backfill(symbol, start, end, strike_offsets=range(-3, 4),
                                 interval=5, expiry_flag="MONTH", store=view)
            registry.record_event("info", "scanner",
                                  f"backfill {symbol} {start}..{end} done")
        except Exception as e:
            registry.record_event("warn", "scanner", f"backfill {symbol} failed: {e!r}")

    threading.Thread(target=_job, daemon=True).start()
    return {"status": "started", "symbol": symbol, "days": days}


@scanner_router.get("/scanner/journal")
def scanner_journal(limit: int = 200, symbol: str = "", kind: str = ""):
    """The rich per-trade journal (newest first): every entry/exit with the
    full setup context, prices, times and MFE/MAE excursions."""
    return {"rows": registry.journal_rows(limit=min(int(limit), 2000),
                                          symbol=symbol.upper(), kind=kind)}


@scanner_router.get("/scanner/insights")
def scanner_insights():
    """Aggregated evidence from the closed-trade journal (win rate, expectancy
    by score band / entry hour / buildup / exit reason, giveback, churn) plus
    data-backed suggestions for improving the trading strategy."""
    return scanner_engine.trader.reflect()


@scanner_router.get("/scanner/trades")
def scanner_trades():
    """The positional paper book the scanner is trading: open positions with
    live MTM + trailing stop, plus realized/unrealized P&L."""
    return scanner_engine.trader.snapshot()


@scanner_router.get("/scanner/{symbol}")
def scanner_detail(symbol: str):
    """Tier-1 + Tier-2 detail for one symbol."""
    return scanner_engine.detail(symbol.upper())


@scanner_router.post("/scanner/settings")
def set_scanner(req: ScannerSettingsReq):
    if req.enabled is not None:
        registry.set_setting("scanner", "on" if req.enabled else "off")
    if req.alert_score is not None:
        registry.set_setting("scanner_alert_score",
                             str(max(0.0, min(100.0, req.alert_score))))
    if req.record_chains is not None:
        registry.set_setting("scanner_record_chains",
                             "on" if req.record_chains else "off")
    registry.record_event("info", "scanner", "Scanner settings updated")
    return scanner_view()


class TraderSettingsReq(BaseModel):
    enabled: bool | None = None
    entry_score: float | None = None
    exit_score: float | None = None
    trail_pct: float | None = None
    hard_stop_pct: float | None = None
    target_pct: float | None = None
    risk_pct: float | None = None
    max_positions: int | None = None
    capital: float | None = None


@scanner_router.post("/scanner/trade-settings")
def set_trader(req: TraderSettingsReq):
    if req.enabled is not None:
        registry.set_setting("scanner_trade", "on" if req.enabled else "off")
    for key, val, lo, hi in [
        ("scanner_trade_entry_score", req.entry_score, 0, 100),
        ("scanner_trade_exit_score", req.exit_score, 0, 100),
        ("scanner_trade_trail_pct", req.trail_pct, 0.01, 0.9),
        ("scanner_trade_hard_stop_pct", req.hard_stop_pct, 0.01, 0.9),
        ("scanner_trade_target_pct", req.target_pct, 0.0, 10.0),
        ("scanner_trade_risk_pct", req.risk_pct, 0.001, 0.2),
        ("scanner_trade_max_positions", req.max_positions, 1, 50),
        ("scanner_trade_capital", req.capital, 0.0, 1e9),
    ]:
        if val is not None:
            registry.set_setting(key, str(max(lo, min(hi, val))))
    registry.record_event("info", "scanner", "Scanner trader settings updated")
    return scanner_engine.trader.snapshot()
