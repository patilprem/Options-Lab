"""
Verify the index-futures VOLUME companion (step 4, gated off by default)
=======================================================================
Read-only market-data check — NO orders, no ledger writes. Run ON THE VPS
(static IP registered, creds present) DURING NSE market hours to confirm the
three things that couldn't be verified offline before flipping the
`index_futures_volume` setting on:

  1. resolve_index_futures() picks each index's real front-month FUTIDX id.
  2. The MarketFeed mode ints hardcoded in paper.py match the SDK's constants
     (_FEED_FULL / _FEED_NSE_FNO / _FEED_BSE_FNO / _FEED_TICKER / _FEED_IDX).
  3. A Full subscription to that future actually delivers a CUMULATIVE
     `volume` field (that increases) and an OI field — the inputs
     LiveFeed._volume_delta() turns into per-bar volume/OI.

Usage (on the VPS, market hours):
    venv/bin/python -m scripts.verify_index_futures            # NIFTY, BANKNIFTY
    venv/bin/python -m scripts.verify_index_futures --symbols NIFTY --seconds 60

Exit code 0 = all three checks passed; nonzero = something to fix (the output
tells you what). Nothing here changes app state.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime

from app.data import dhan_client
from app.engines.paper import (_FEED_BSE_FNO, _FEED_FULL, _FEED_IDX,
                               _FEED_NSE_FNO, _FEED_TICKER)


def check_constants() -> bool:
    """Compare paper.py's hardcoded feed-mode ints against the SDK's own."""
    from dhanhq import MarketFeed
    pairs = [
        ("Full (companion future)", _FEED_FULL, getattr(MarketFeed, "Full", None)),
        ("NSE_FNO segment", _FEED_NSE_FNO, getattr(MarketFeed, "NSE_FNO", None)),
        ("BSE_FNO segment", _FEED_BSE_FNO, getattr(MarketFeed, "BSE_FNO", None)),
        ("Ticker (spot)", _FEED_TICKER, getattr(MarketFeed, "Ticker", None)),
        ("IDX segment", _FEED_IDX, getattr(MarketFeed, "IDX", None)),
    ]
    ok = True
    print("\n[2] Feed-mode constants (paper.py vs dhanhq SDK)")
    for name, ours, sdk in pairs:
        match = (sdk is not None and int(sdk) == ours)
        ok = ok and match
        flag = "OK " if match else "MISMATCH"
        print(f"    {flag}  {name}: ours={ours}  sdk={sdk}")
    if not ok:
        print("    -> update the _FEED_* constants in app/engines/paper.py to the "
              "SDK values above.")
    return ok


def check_resolution(symbols: list[str]) -> dict:
    print("[1] resolve_index_futures() — front-month FUTIDX ids")
    resolved = dhan_client.resolve_index_futures()
    if not resolved:
        print("    EMPTY — no FUTIDX rows matched. Check the scrip master parse "
              "(instrument name / trading-symbol format) in parse_index_futures().")
        return {}
    today = datetime.now(dhan_client.IST).date().isoformat()
    for name in symbols:
        fut = resolved.get(name)
        if not fut:
            print(f"    MISSING  {name}: not resolved")
            continue
        future = fut["expiry"] >= today
        flag = "OK " if future else "STALE"
        print(f"    {flag}  {name}: security_id={fut['security_id']} "
              f"expiry={fut['expiry']} segment={fut['segment']}")
    print("    -> cross-check a security_id against the Dhan app/web's current "
          "front-month future for that index.")
    return resolved


async def check_live_packets(resolved: dict, symbols: list[str], seconds: int) -> bool:
    """Subscribe each index's SPOT (Ticker) + FUTURE (Full) and watch the raw
    packets for a cumulative, increasing volume + an OI field on the future."""
    from dhanhq import MarketFeed

    instruments = []
    fut_sids, spot_sids = {}, {}
    for name in symbols:
        cfg = dhan_client.UNDERLYINGS.get(name)
        if cfg:
            instruments.append((_FEED_IDX, str(cfg["security_id"]), _FEED_TICKER))
            spot_sids[int(cfg["security_id"])] = name
        fut = resolved.get(name)
        if fut:
            seg = (_FEED_BSE_FNO
                   if (cfg or {}).get("fno_segment") == "BSE_FNO"
                   else _FEED_NSE_FNO)
            instruments.append((seg, str(fut["security_id"]), _FEED_FULL))
            fut_sids[int(fut["security_id"])] = name
    if not fut_sids:
        print("\n[3] SKIPPED — no futures resolved to subscribe.")
        return False

    print(f"\n[3] Live Full packets for {seconds}s "
          f"(futures: {', '.join(fut_sids.values())}) ...")
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
            if sid is None:
                continue
            sid = int(sid)
            if sid not in fut_sids:
                continue
            samples += 1
            vol = pkt.get("volume")
            oi = pkt.get("OI", pkt.get("oi", pkt.get("open_interest")))
            if vol is not None:
                first_vol.setdefault(sid, vol)
                last_vol[sid] = vol
            if oi is not None:
                saw_oi[sid] = oi
            if samples <= 3:                     # show a few raw packets
                print(f"    raw[{fut_sids[sid]}]: keys={sorted(pkt.keys())}")
    finally:
        try:
            await feed.disconnect()
        except Exception:
            pass

    ok = True
    print(f"    got {samples} future packets")
    for sid, name in fut_sids.items():
        has_vol = sid in last_vol
        grew = has_vol and last_vol[sid] >= first_vol[sid]
        has_oi = sid in saw_oi
        ok = ok and has_vol and grew and has_oi
        print(f"    {name}: volume_field={'yes' if has_vol else 'NO'} "
              f"(first={first_vol.get(sid)} last={last_vol.get(sid)}, "
              f"increasing={'yes' if grew else 'NO'})  "
              f"OI_field={'yes' if has_oi else 'NO'} ({saw_oi.get(sid)})")
    if not ok:
        print("    -> if volume/OI are absent, the Full packet uses different "
              "keys; adjust LiveFeed._volume_delta() key lookups.")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", nargs="+", default=["NIFTY", "BANKNIFTY"])
    ap.add_argument("--seconds", type=int, default=45)
    args = ap.parse_args()

    print("=" * 70)
    print("Index-futures volume companion — verification (read-only)")
    print("=" * 70)
    resolved = check_resolution(args.symbols)
    consts_ok = check_constants()
    try:
        live_ok = asyncio.run(check_live_packets(resolved, args.symbols, args.seconds))
    except Exception as e:
        print(f"\n[3] live check failed to run: {e!r}")
        print("    (needs creds + static IP + market hours)")
        live_ok = False

    passed = bool(resolved) and consts_ok and live_ok
    print("\n" + "=" * 70)
    print("VERDICT:", "ALL CHECKS PASSED — safe to set index_futures_volume=on"
          if passed else "NOT READY — fix the items flagged above first")
    print("=" * 70)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
