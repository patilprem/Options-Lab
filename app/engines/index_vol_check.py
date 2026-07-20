"""
Index-futures volume companion — self-check (step 4 residual)
=============================================================
The one thing that can't be verified off the VPS: that a FULL subscription to
each index's front-month future actually STREAMS increasing day-volume + OI
from the registered static IP during market hours. This module runs that check
IN-PROCESS on the VPS (no shell needed) and reports the verdict to the events
log + an ntfy phone push, so you learn whether it's safe to flip
`index_futures_volume` on without ever logging in.

It is read-only: it opens its OWN short-lived dhanhq MarketFeed (isolated from
the trading feed, ~45s) purely to observe packet shape. The three checks match
scripts/verify_index_futures.py — this is the shared source of truth the script
imports, so CLI and in-app runs stay identical.

Scheduling lives in MarketHub._index_vol_check_loop: once per market day until
it records a pass (settings: index_vol_check = auto|off|force,
index_vol_check_result, index_vol_check_ran).
"""

from __future__ import annotations

import asyncio


def check_constants() -> tuple[bool, list[str]]:
    """Feed-mode/segment ints in paper.py vs the installed SDK's constants."""
    lines = ["[2] Feed-mode constants (paper.py vs dhanhq SDK)"]
    try:
        from dhanhq import MarketFeed
    except Exception as e:
        return False, lines + [f"    SDK import failed: {e!r}"]
    from app.engines.paper import (_FEED_BSE_FNO, _FEED_FULL, _FEED_IDX,
                                   _FEED_NSE_FNO, _FEED_TICKER)
    pairs = [
        ("Full (companion)", _FEED_FULL, getattr(MarketFeed, "Full", None)),
        ("NSE_FNO", _FEED_NSE_FNO, getattr(MarketFeed, "NSE_FNO", None)),
        ("BSE_FNO", _FEED_BSE_FNO, getattr(MarketFeed, "BSE_FNO", None)),
        ("Ticker", _FEED_TICKER, getattr(MarketFeed, "Ticker", None)),
        ("IDX", _FEED_IDX, getattr(MarketFeed, "IDX", None)),
    ]
    ok = True
    for name, ours, sdk in pairs:
        match = (sdk is not None and int(sdk) == ours)
        ok = ok and match
        lines.append(f"    {'OK ' if match else 'MISMATCH'}  {name}: "
                     f"ours={ours} sdk={sdk}")
    return ok, lines


def check_resolution(symbols) -> tuple[dict, list[str]]:
    """resolve_index_futures() picks each index's front-month FUTIDX id."""
    from app.data import dhan_client
    from datetime import datetime
    lines = ["[1] resolve_index_futures() — front-month FUTIDX ids"]
    try:
        resolved = dhan_client.resolve_index_futures()
    except Exception as e:
        return {}, lines + [f"    resolve failed: {e!r}"]
    if not resolved:
        return {}, lines + ["    EMPTY — no FUTIDX rows matched; check "
                            "parse_index_futures() symbol format"]
    today = datetime.now(dhan_client.IST).date().isoformat()
    for name in symbols:
        fut = resolved.get(name)
        if not fut:
            lines.append(f"    MISSING  {name}")
            continue
        flag = "OK " if fut["expiry"] >= today else "STALE"
        lines.append(f"    {flag}  {name}: id={fut['security_id']} "
                     f"expiry={fut['expiry']} seg={fut['segment']}")
    return resolved, lines


async def observe_futures(resolved: dict, symbols, seconds: int) -> tuple[bool, list[str]]:
    """Subscribe each index's SPOT (Ticker) + FUTURE (Full) and confirm the
    future packets carry a cumulative, increasing `volume` and an `OI` field."""
    from dhanhq import MarketFeed

    from app.data import dhan_client
    from app.engines.paper import (_FEED_BSE_FNO, _FEED_FULL, _FEED_IDX,
                                   _FEED_NSE_FNO, _FEED_TICKER)
    lines = [f"[3] Live Full packets for {seconds}s"]
    instruments, fut_sids = [], {}
    for name in symbols:
        cfg = dhan_client.UNDERLYINGS.get(name)
        if cfg:
            instruments.append((_FEED_IDX, str(cfg["security_id"]), _FEED_TICKER))
        fut = resolved.get(name)
        if fut:
            seg = (_FEED_BSE_FNO if (cfg or {}).get("fno_segment") == "BSE_FNO"
                   else _FEED_NSE_FNO)
            instruments.append((seg, str(fut["security_id"]), _FEED_FULL))
            fut_sids[int(fut["security_id"])] = name
    if not fut_sids:
        return False, lines + ["    SKIPPED — no futures resolved"]

    feed = MarketFeed(dhan_client.get_dhan_context(), instruments, "v2")
    await feed.connect()
    first_vol, last_vol, saw_oi = {}, {}, {}
    samples = 0
    loop = asyncio.get_event_loop()
    deadline = loop.time() + seconds
    try:
        while loop.time() < deadline and not feed._is_ws_closed():
            try:
                pkt = await asyncio.wait_for(feed.get_instrument_data(), timeout=10)
            except asyncio.TimeoutError:
                continue
            if not isinstance(pkt, dict):
                continue
            sid = pkt.get("security_id")
            if sid is None or int(sid) not in fut_sids:
                continue
            sid = int(sid)
            samples += 1
            vol = pkt.get("volume")
            oi = pkt.get("OI", pkt.get("oi", pkt.get("open_interest")))
            if vol is not None:
                first_vol.setdefault(sid, vol)
                last_vol[sid] = vol
            if oi is not None:
                saw_oi[sid] = oi
    finally:
        try:
            await feed.disconnect()
        except Exception:
            pass

    lines.append(f"    got {samples} future packets")
    ok = bool(fut_sids)
    for sid, name in fut_sids.items():
        has_vol = sid in last_vol
        grew = has_vol and last_vol[sid] >= first_vol.get(sid, last_vol[sid])
        has_oi = sid in saw_oi
        ok = ok and has_vol and grew and has_oi
        lines.append(f"    {name}: volume={'y' if has_vol else 'NO'} "
                     f"(first={first_vol.get(sid)} last={last_vol.get(sid)} "
                     f"inc={'y' if grew else 'NO'}) OI={'y' if has_oi else 'NO'} "
                     f"({saw_oi.get(sid)})")
    return ok, lines


def _observe_sync(resolved: dict, symbols, seconds: int) -> tuple[bool, list[str]]:
    """Run observe_futures on a fresh event loop — safe from a plain worker
    thread (no running loop), the way the app calls it via run_in_executor."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(observe_futures(resolved, symbols, seconds))
    finally:
        loop.close()


def run_full_check(symbols, seconds: int = 45) -> dict:
    """All three checks. Returns {passed, verdict, lines, resolved}."""
    resolved, l1 = check_resolution(symbols)
    consts_ok, l2 = check_constants()
    lines = l1 + l2
    if resolved:
        try:
            live_ok, l3 = _observe_sync(resolved, symbols, seconds)
        except Exception as e:
            live_ok, l3 = False, [f"[3] live check failed to run: {e!r}"]
    else:
        live_ok, l3 = False, ["[3] SKIPPED — nothing resolved"]
    lines += l3
    passed = bool(resolved) and consts_ok and live_ok
    return {"passed": passed, "verdict": "PASS" if passed else "FAIL",
            "lines": lines, "resolved": resolved}


# Substrings that mark a FAILING line in run_full_check's output — used to pull
# the actual reason into the phone push (a bare "See Activity log" is useless
# when the log lives on the VPS and you're holding your phone).
_FAIL_MARKERS = ("MISMATCH", "MISSING", "STALE", "EMPTY", "SKIPPED",
                 "failed", "volume=NO", "inc=NO", "OI=NO", "got 0 ")


def _fail_reason(lines) -> str:
    """One compact line naming what actually failed, for the FAIL push. Prefers
    the flagged lines (constant mismatch / stale id / no volume|OI); falls back
    to the last line so the push is never empty."""
    hits = [ln.strip() for ln in lines
            if any(m in ln for m in _FAIL_MARKERS)]
    reason = "; ".join(hits[:2]) if hits else (lines[-1].strip() if lines else "")
    return reason[:180]


def run_and_report(symbols, seconds: int = 45) -> dict:
    """Run the full check and REPORT: events log + ntfy push + persisted result.
    Sync — call via run_in_executor so the socket work stays off the app loop."""
    from datetime import datetime

    from app.core import registry
    from app.data.dhan_client import IST
    from app.engines.watchdog import push_ntfy

    res = run_full_check(symbols, seconds)
    for ln in res["lines"]:
        registry.record_event("info", "diag", ln)
    day = datetime.now(IST).date().isoformat()
    tail = res["lines"][-1] if res["lines"] else ""
    registry.set_setting("index_vol_check_result",
                         f"{'pass' if res['passed'] else 'fail'} {day} :: {tail}")
    if res["passed"]:
        msg = ("index-futures volume self-check: PASS. "
               "Safe to set index_futures_volume=on.")
    else:
        # Carry the concrete failure into the push so it's diagnosable from the
        # phone without opening the Activity log on the VPS.
        msg = ("index-futures volume self-check: FAIL — "
               + (_fail_reason(res["lines"]) or "see Activity log for details."))
    registry.record_event("info" if res["passed"] else "warn", "diag", msg)
    try:
        push_ntfy(msg, "recovered" if res["passed"] else "down")
    except Exception:
        pass
    return res
