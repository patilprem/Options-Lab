from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import asyncio


class SPAStaticFiles(StaticFiles):
    """Serve the SPA with correct cache policy so pushes show up immediately:
    - index.html: `no-cache` → the browser always revalidates, so a new build's
      hashed asset names are picked up on the next refresh (no stale UI, no
      cache-busting query tricks).
    - /assets/* (content-hashed): cache forever — the filename changes on rebuild.
    """
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        ctype = resp.headers.get("content-type", "")
        if ctype.startswith("text/html"):
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        elif "assets/" in path.replace("\\", "/"):   # normpath uses \ on Windows
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp

from app.core import registry, token_manager
from app.api.strategies import (router, portfolio_router, activity_router,
                                data_router, trades_router, risk_router,
                                live_router, scanner_router, diag_router, hub,
                                runner, scanner_engine, _instantiate)

registry.init_db()
app = FastAPI(title="OptionsLab", version="1.0.0")
# scanner_engine (FNO stock scanner, F2/F3/F4) is created in app.api.strategies
# alongside hub/runner and imported above; its poll loops are no-ops until the
# `scanner` setting is turned "on" (needs live creds on the VPS).

# Include all routers
app.include_router(router)
app.include_router(portfolio_router)
app.include_router(activity_router)
app.include_router(data_router)
app.include_router(trades_router)
app.include_router(risk_router)
app.include_router(live_router)
app.include_router(scanner_router)
app.include_router(diag_router)
app.include_router(token_manager.router)

# Static files + SPA fallback
STATIC = Path(__file__).resolve().parent / "static"

def _session_open(name: str, now=None) -> bool:
    """Is `name`'s exchange session open (IST)? Recording outside the session
    would re-stamp Friday's frozen chain as fresh data all weekend (observed:
    ~117k junk rows in one Sunday). NSE 09:15-15:35, MCX 09:00-23:35, Mon-Fri."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    ist = now or _dt.now(_tz(_td(hours=5, minutes=30)))
    if ist.weekday() >= 5:
        return False
    mins = ist.hour * 60 + ist.minute
    from app.data.dhan_client import MCX_DYNAMIC
    if name in MCX_DYNAMIC:
        return 9 * 60 <= mins <= 23 * 60 + 35
    return 9 * 60 + 15 <= mins <= 15 * 60 + 35


def _record_list() -> list[str]:
    """Underlyings the live recorder captures (settings-driven). Merges the
    edge-research list with the legacy MCX list."""
    rec = [u.strip() for u in registry.setting(
        "record_underlyings", "NIFTY,BANKNIFTY").split(",") if u.strip()]
    mcx = [u.strip() for u in registry.setting(
        "mcx_underlyings", "CRUDEOIL,GOLD").split(",") if u.strip()]
    return sorted(set(rec) | set(mcx))


async def _market_recorder():
    """Every 5 minutes, persist the live market picture for strategy research:
      * chain_snapshots — FULL chain fidelity per strike: ltp/bid/ask, IV, OI,
        volume, greeks, spot (the institutional-footprint dataset; the
        historical backfill has none of this)
      * option_bars     — ATM-relative ltp snapshots (backtest-compatible;
        also builds the expired-options history Dhan doesn't provide for MCX)
    Uses the MarketHub chain poller (M3). Freshness-guarded, so off-hours
    ticks don't restamp stale prices. Needs a real DataStore + valid token;
    MCX names also need MCX security ids in dhan_client.UNDERLYINGS."""
    loop = asyncio.get_running_loop()
    delay = 15    # first pass right after boot: registers MCX names with the
    while True:   # feed (canary + recorder) instead of waiting out a full tick
        await asyncio.sleep(delay)
        delay = 300
        if registry.setting("recording", "on") != "on":
            continue
        store = hub.store
        if not hasattr(store, "con"):
            continue  # synthetic store — nothing to persist into
        names = _record_list()
        try:
            # MCX chains hang off the CURRENT futures contract (id rolls
            # monthly) — resolve from the scrip master before polling.
            from app.data.dhan_client import MCX_DYNAMIC, UNDERLYINGS, resolve_mcx_ids
            from datetime import datetime as _dt
            _today = _dt.now().date().isoformat()
            if any(n in MCX_DYNAMIC and (
                    n not in UNDERLYINGS
                    or UNDERLYINGS[n].get("expiry", "9999") < _today)  # rolled
                   for n in names):
                old_ids = {n: UNDERLYINGS.get(n, {}).get("security_id")
                           for n in MCX_DYNAMIC}
                try:
                    ids = await loop.run_in_executor(None, resolve_mcx_ids)
                    if ids:
                        registry.record_event("info", "feed", f"MCX ids resolved: {ids}")
                        rolled = [n for n, sid in ids.items()
                                  if old_ids.get(n) is not None and old_ids[n] != sid]
                        if rolled:
                            # the WS feed is still subscribed to the OLD (now
                            # dead) contract's security id — force a reconnect
                            # so it picks up the new one, else this
                            # underlying's live recording silently stalls
                            hub.resubscribe()
                            registry.record_event(
                                "info", "feed",
                                f"MCX contract rolled ({rolled}); feed resubscribed")
                except Exception as e:
                    registry.record_event("warn", "feed", f"MCX resolve failed: {e!r}")
            for u in names:
                hub.enable_chain(u)          # ensure it's being polled
            await hub.ensure_started()        # start the feed/poller if idle
            live_names = [u for u in names if _session_open(u)]
            # holiday/frozen-chain guard: persist only underlyings whose chain
            # CONTENT moved since the last snapshot (weekday holidays pass the
            # clock gate but serve a frozen chain — identical fingerprint)
            changed = hub.chain_changed(live_names)
            if not changed:
                continue                      # closed or frozen — nothing real
            n_full = hub.persist_chain_full(store, underlyings=changed)
            n_atm = hub.persist_chain_snapshots(store, underlyings=changed)
            hub.mark_chain_persisted(changed)
            if n_full or n_atm:
                registry.record_event("info", "feed",
                                       f"recorder: {n_full} chain rows + {n_atm} ATM snapshots")
        except Exception as e:
            registry.record_event("warn", "feed", f"recorder error: {e!r}")


async def _spot_bar_recorder():
    """Persist every completed live 5-min candle into underlying_bars so the
    store stays current day-by-day (no re-backfill needed to include today)."""
    store = hub.store
    if not hasattr(store, "con"):
        return
    q = hub.subscribe()
    while True:
        msg = await q.get()
        try:
            kind, underlying, interval, bar = msg
            if kind == "bar" and interval == 5 and bar is not None:
                store.upsert_live_bar(underlying, bar)
        except Exception as e:
            registry.record_event("warn", "feed", f"spot recorder error: {e!r}")

async def _nightly_gap_repair():
    """Self-healing dataset: every weekday at 16:10 IST, re-backfill the
    trailing week for the NSE record list. The chunk ledger makes this
    cheap — completed chunks are skipped, so only holes (restart windows,
    incident days like 2026-07-13's frozen chain, expired-option data that
    only becomes available after expiry) are actually fetched. Research
    data should not depend on a human remembering to patch it."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz, date as _date
    from app.data.dhan_client import UNDERLYINGS, MCX_DYNAMIC
    ist = _tz(_td(hours=5, minutes=30))
    while True:
        now = _dt.now(ist)
        target = now.replace(hour=16, minute=10, second=0, microsecond=0)
        if now >= target:
            target += _td(days=1)
        while target.weekday() >= 5:          # skip weekends
            target += _td(days=1)
        await asyncio.sleep((target - now).total_seconds())
        if registry.setting("gap_repair", "on") != "on":
            continue
        if not hasattr(hub.store, "con"):
            continue                          # synthetic store
        if registry.get_backfill_status().get("running"):
            registry.record_event("info", "data",
                                  "gap repair skipped: a backfill is running")
            continue
        end = _date.today()
        start = end - _td(days=7)
        names = [u for u in _record_list()
                 if u in UNDERLYINGS and u not in MCX_DYNAMIC]  # NSE/BSE only
        for u in names:
            try:
                from app.data import backfill_job
                view = type("V", (), {"con": hub.store.con.cursor()})()
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, lambda u=u: backfill_job.run(
                    u, start, end, strike_offsets=2, interval=5, store=view))
                registry.record_event("info", "data",
                                      f"gap repair: {u} {start} .. {end} done")
            except Exception as e:
                registry.record_event("warn", "data",
                                      f"gap repair failed [{u}]: {e!r}")
        # F5: score today's recorded index bias vs the realized move now that
        # the session (and its spot bars) is complete.
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, scanner_engine.score_yesterday_bias, end)
        except Exception as e:
            registry.record_event("warn", "scanner", f"bias scoring failed: {e!r}")


async def _nightly_maintenance():
    """Keep the market-data store lean without a human in the loop. Every day
    at 02:00 IST — both NSE and MCX shut, so no contention with live recording
    or the chain poller — purge off-hours/weekend JUNK rows (never real
    session data; see DataStore.purge_offhours) and CHECKPOINT so DuckDB
    actually reclaims the freed disk. Gated by the `db_maintenance` setting."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    ist = _tz(_td(hours=5, minutes=30))
    while True:
        now = _dt.now(ist)
        target = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if now >= target:
            target += _td(days=1)
        await asyncio.sleep((target - now).total_seconds())
        if registry.setting("db_maintenance", "on") != "on":
            continue
        store = hub.store
        if not hasattr(store, "purge_offhours"):
            continue                          # synthetic store — nothing to do
        loop = asyncio.get_running_loop()
        try:
            res = await loop.run_in_executor(None, store.purge_offhours)
            deleted = sum(v.get("deleted", 0) for v in res.values())
            await loop.run_in_executor(None, store.checkpoint)
            registry.record_event("info", "data",
                                  f"db maintenance: purged {deleted} junk rows, "
                                  f"checkpointed ({res})")
        except Exception as e:
            registry.record_event("warn", "data", f"db maintenance failed: {e!r}")


@app.on_event("startup")
async def startup_event():
    token_task = asyncio.create_task(token_manager.daily_refresh_loop())
    rec_task = asyncio.create_task(_market_recorder())
    spot_task = asyncio.create_task(_spot_bar_recorder())
    repair_task = asyncio.create_task(_nightly_gap_repair())
    maint_task = asyncio.create_task(_nightly_maintenance())
    scanner_task = asyncio.create_task(scanner_engine.run())
    scanner_t2_task = asyncio.create_task(scanner_engine.run_tier2(hub))
    registry.record_event("info", "engine", "OptionsLab started")
    # M4: recover any paper strategies that were live before a restart.
    try:
        await runner.restore_all(_instantiate)
    except Exception as e:
        registry.record_event("error", "engine", f"paper restore_all failed: {e!r}")

# Mount React build at root, with fallback to index.html for SPA routing
if STATIC.exists():
    app.mount("/", SPAStaticFiles(directory=str(STATIC), html=True), name="static")
else:
    # Fallback during development
    @app.get("/{full_path:path}")
    async def catch_all(full_path: str):
        index = STATIC.parent / "index.html"
        if index.exists():
            return FileResponse(str(index))
        return {"detail": "Build the frontend: cd frontend && npm install && npm run build"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
