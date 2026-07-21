"""
DhanHQ Data Adapter
===================
Populates ``marketdata.duckdb`` from DhanHQ v2 historical APIs, via the
official ``dhanhq`` Python SDK (``pip install dhanhq``). The SDK wraps every
HTTP response as ``{"status", "remarks", "data"}`` and shields us from raw
endpoint churn.

SDK methods used:
  intraday_minute_data   underlying candles (1/5/15/25/60 min, up to 5 yrs,
                         fetch <=90 days per call — loop and store locally)
  expired_options_data   expired options: minute OHLC/IV/OI by ATM-relative
                         strike, <=30 days per call, up to 5 yrs (NSE/BSE;
                         verify MCX support). Arrays nest under data["ce"]
                         and data["pe"].
  option_chain           live chain: greeks/IV/bid-ask (1 req / 3 s)  [M3]

Credentials come from the environment (never hardcode — SEBI caps access
tokens at 24 h, so the running server refreshes them via token_manager):
  DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN     — or —
  DHAN_CONFIG_PATH -> {"client_id": ..., "access_token": ...}

Run:
  python -m app.data.dhan_client backfill NIFTY 2024-01-01 2025-01-01

Design note: fetching (needs the SDK + a live token) is kept separate from
parsing/upserting (pure dict -> rows). The parse helpers below are exercised
offline by tests/test_dhan_parsing.py replaying saved sample responses, so
the storage boundary can be verified without network or credentials.
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# IST is UTC+5:30. Dhan historical APIs return epoch seconds; we convert to
# a naive IST datetime at this boundary so the store matches everything else
# in the app (invariant #7: user-facing timestamps are IST).
IST = timezone(timedelta(hours=5, minutes=30))

# Friendly name -> Dhan security IDs / segments (extend from the security
# master CSV — see dhanhq.fetch_security_list). security_id + IDX_I identify
# the spot index; fno_segment + instrument identify its option contracts.
UNDERLYINGS = {
    "NIFTY":     {"security_id": 13,  "segment": "IDX_I", "fno_segment": "NSE_FNO", "instrument": "OPTIDX"},
    "BANKNIFTY": {"security_id": 25,  "segment": "IDX_I", "fno_segment": "NSE_FNO", "instrument": "OPTIDX"},
    "FINNIFTY":  {"security_id": 27,  "segment": "IDX_I", "fno_segment": "NSE_FNO", "instrument": "OPTIDX"},
    "SENSEX":    {"security_id": 51,  "segment": "IDX_I", "fno_segment": "BSE_FNO", "instrument": "OPTIDX"},
    # MCX names are injected at runtime by resolve_mcx_ids(): a commodity
    # chain hangs off the CURRENT futures contract, whose security id rolls
    # every month — it must be resolved from the scrip master, not hardcoded.
}

# MCX commodity name -> trading-symbol prefix in the scrip master. The big
# contract is wanted; the mini ("...M") is filtered out by exact prefix match.
MCX_DYNAMIC = {"CRUDEOIL": "CRUDEOIL", "GOLD": "GOLD"}
_SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
_MASTER_CACHE = Path(__file__).resolve().parents[2] / "scrip_master_mcx.csv"
# The FNO-stock universe is trimmed from the SAME master, kept separately so
# its 24h refresh cadence is independent of the MCX one.
_FNO_MASTER_CACHE = Path(__file__).resolve().parents[2] / "scrip_master_fno.csv"
# Index FUTURES (FUTIDX) get their OWN trim. They MUST NOT share
# _FNO_MASTER_CACHE: resolve_fno_universe writes that file keeping only
# FUTSTK/EQUITY rows (every FUTIDX stripped) and the scanner refreshes it
# constantly — so a shared cache leaves resolve_index_futures reading a file
# that never held a single FUTIDX row (observed 2026-07-20:
# "[1] EMPTY — no FUTIDX rows matched" despite a correct symbol format).
_IDX_FUT_MASTER_CACHE = Path(__file__).resolve().parents[2] / "scrip_master_idxfut.csv"


def resolve_mcx_ids(max_age_h: float = 24.0) -> dict:
    """Resolve each MCX_DYNAMIC name to its nearest non-expired FUTCOM
    contract via Dhan's public scrip master (cached locally; only the MCX
    rows are kept — the full file is ~27 MB). Injects/updates UNDERLYINGS
    entries and returns {name: security_id}. Safe to call repeatedly."""
    import csv
    import io
    import urllib.request

    if (not _MASTER_CACHE.exists()
            or time.time() - _MASTER_CACHE.stat().st_mtime > max_age_h * 3600):
        raw = urllib.request.urlopen(_SCRIP_MASTER_URL, timeout=120).read() \
            .decode("utf-8", "replace")
        lines = raw.splitlines()
        keep = [lines[0]] + [ln for ln in lines[1:] if ln.startswith("MCX,")]
        _MASTER_CACHE.write_text("\n".join(keep), encoding="utf-8")

    rows = list(csv.DictReader(io.StringIO(
        _MASTER_CACHE.read_text(encoding="utf-8"))))
    today = datetime.now(IST).strftime("%Y-%m-%d")
    out = {}
    for name, prefix in MCX_DYNAMIC.items():
        futs = [r for r in rows
                if r.get("SEM_INSTRUMENT_NAME") == "FUTCOM"
                and (r.get("SEM_TRADING_SYMBOL") or "").startswith(prefix + "-")
                and (r.get("SEM_EXPIRY_DATE") or "") >= today]
        if not futs:
            continue
        fut = min(futs, key=lambda r: r["SEM_EXPIRY_DATE"])
        sid = int(fut["SEM_SMST_SECURITY_ID"])
        UNDERLYINGS[name] = {"security_id": sid, "segment": "MCX_COMM",
                             "fno_segment": "MCX_COMM", "instrument": "OPTFUT",
                             "expiry": fut["SEM_EXPIRY_DATE"][:10]}
        out[name] = sid
    return out


# Index names whose front-month FUTIDX supplies the traded volume/OI their
# spot (IDX_I) index can't — the companion feed for volume-based price action.
INDEX_FUT_NAMES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
                   "SENSEX", "BANKEX")


def parse_index_futures(rows: list, names, today: str) -> dict:
    """Pure: pick each index's nearest non-expired FUTIDX contract from scrip-
    master rows. Returns {name: {security_id, expiry, segment}}. Matched on the
    trading symbol with a boundary check so NIFTY never captures NIFTYNXT50.
    Tested offline; resolve_index_futures() feeds it the real master."""
    out = {}
    for name in names:
        cands = []
        for r in rows:
            if r.get("SEM_INSTRUMENT_NAME") != "FUTIDX":
                continue
            sym = (r.get("SEM_TRADING_SYMBOL") or "").upper()
            if not sym.startswith(name):
                continue
            tail = sym[len(name):len(name) + 1]
            if tail and tail.isalnum():          # e.g. NIFTYNXT50 for NIFTY
                continue
            if (r.get("SEM_EXPIRY_DATE") or "") >= today:
                cands.append(r)
        if not cands:
            continue
        fut = min(cands, key=lambda r: r["SEM_EXPIRY_DATE"])
        out[name] = {"security_id": int(fut["SEM_SMST_SECURITY_ID"]),
                     "expiry": (fut.get("SEM_EXPIRY_DATE") or "")[:10],
                     "segment": (fut.get("SEM_SEGMENT")
                                 or fut.get("SEM_EXM_EXCH_ID") or "NSE")}
    return out


def resolve_index_futures(max_age_h: float = 24.0) -> dict:
    """Resolve each index's front-month FUTIDX security id from Dhan's scrip
    master. Returns {name: {security_id, expiry, segment}}. Safe to call
    repeatedly. Network/cache wrapper around the pure parse_index_futures().

    Uses a DEDICATED cache (_IDX_FUT_MASTER_CACHE) trimmed to FUTIDX rows only —
    it deliberately does NOT reuse _FNO_MASTER_CACHE, which the scanner rewrites
    with FUTSTK/EQUITY rows only and would leave this returning EMPTY. Live-
    verify on the VPS: the FUTIDX trading-symbol format and the NSE-FnO int."""
    import csv
    import io
    import urllib.request

    if (not _IDX_FUT_MASTER_CACHE.exists()
            or time.time() - _IDX_FUT_MASTER_CACHE.stat().st_mtime > max_age_h * 3600):
        raw = urllib.request.urlopen(_SCRIP_MASTER_URL, timeout=120).read() \
            .decode("utf-8", "replace")
        lines = raw.splitlines()
        # NSE/BSE index-future rows only (SENSEX/BANKEX futures trade on BSE);
        # "FUTIDX" gates to index futures, mirroring resolve_fno_universe's
        # "FUTSTK" in ln idiom and keeping the cache tiny.
        keep = [lines[0]] + [ln for ln in lines[1:]
                             if (ln.startswith("NSE,") or ln.startswith("BSE,"))
                             and "FUTIDX" in ln]
        _IDX_FUT_MASTER_CACHE.write_text("\n".join(keep), encoding="utf-8")
    rows = list(csv.DictReader(io.StringIO(
        _IDX_FUT_MASTER_CACHE.read_text(encoding="utf-8"))))
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return parse_index_futures(rows, INDEX_FUT_NAMES, today)


# ---------------------------------------------------------------------------
# FNO stock universe (scanner) — resolve the ~190 NSE FNO stocks from the
# scrip master, so the scanner knows each name's spot id, current-month
# future id, lot size and expiries. Parsing is pure/offline-testable; the
# download/cache wrapper mirrors resolve_mcx_ids.
# ---------------------------------------------------------------------------

def _to_int(v):
    """'46376', '500.0', 500 -> int; blanks/garbage -> None (lot units and
    security ids arrive as strings, sometimes float-formatted)."""
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def _parse_master_date(v):
    """Scrip-master expiry -> date. Accepts 'YYYY-MM-DD' or
    'YYYY-MM-DD HH:MM:SS' (Dhan has shipped both). None on failure."""
    if not v:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def parse_fno_universe(rows, today=None) -> dict:
    """Pure: scrip-master DictReader rows -> {symbol: {...}} for NSE FNO
    stocks. Groups FUTSTK rows by underlying, keeps only non-expired
    contracts, and pairs each with its cash-equity (spot) security id.

    Returns per symbol: spot_security_id, future_security_id (nearest live
    expiry), fno_segment, lot_size, near_expiry (ISO), expiries (sorted ISO
    list). A symbol with no live future is dropped."""
    if today is None:
        today = datetime.now(IST).date()
    elif isinstance(today, str):
        today = date.fromisoformat(today)

    futs: dict[str, list] = {}   # symbol -> [(expiry_date, sid, lot), ...]
    spots: dict[str, int] = {}   # symbol -> cash-equity security id
    for r in rows:
        exch = (r.get("SEM_EXM_EXCH_ID") or "").strip()
        if exch != "NSE":
            continue
        inst = (r.get("SEM_INSTRUMENT_NAME") or "").strip()
        # SM_SYMBOL_NAME is NOT a reliable join key across row types: real Dhan
        # master data leaves it BLANK on FUTSTK rows (a "RELIANCE-Jul2026-FUT"
        # row) but fills it with the full company name on EQUITY rows ("RELIANCE
        # INDUSTRIES LTD") — joining on that field means spots{} and futs{} never
        # share a key and every stock silently fails to pair with its cash-equity
        # id (2026-07-20: Tier-2 shortlisted 15 movers, 0 resolved). Derive the
        # ticker from SEM_TRADING_SYMBOL instead — for EQUITY it already IS the
        # plain ticker ("RELIANCE"); for FUTSTK it's "RELIANCE-Jul2026-FUT", so
        # split on "-" to strip the expiry/contract suffix.
        ts = (r.get("SEM_TRADING_SYMBOL") or "").strip()
        sym = ts.split("-")[0].strip() if ts else ""
        if not sym:
            continue
        sid = _to_int(r.get("SEM_SMST_SECURITY_ID"))
        if inst == "FUTSTK":
            exp = _parse_master_date(r.get("SEM_EXPIRY_DATE"))
            if exp and sid:
                futs.setdefault(sym, []).append(
                    (exp, sid, _to_int(r.get("SEM_LOT_UNITS"))))
        elif inst == "EQUITY" and sid:
            spots[sym] = sid

    out: dict[str, dict] = {}
    for sym, contracts in futs.items():
        live = sorted(c for c in contracts if c[0] >= today)
        if not live:
            continue
        near_exp, fut_sid, lot = live[0]
        out[sym] = {
            "symbol": sym,
            "spot_security_id": spots.get(sym),
            "future_security_id": fut_sid,
            "fno_segment": "NSE_FNO",
            "lot_size": lot,
            "near_expiry": near_exp.isoformat(),
            "expiries": [c[0].isoformat() for c in live],
        }
    return out


def resolve_fno_universe(max_age_h: float = 24.0, store=None, today=None) -> dict:
    """Download (or reuse a <24h cache of) the scrip master, trim it to NSE
    FUTSTK/EQUITY rows, parse the FNO stock universe, and — if a `store` is
    given — persist a dated snapshot to `fno_universe` (lot sizes accumulate
    over time the way LOT_HISTORY does). Returns {symbol: {...}}."""
    import csv
    import io
    import urllib.request

    if (not _FNO_MASTER_CACHE.exists()
            or time.time() - _FNO_MASTER_CACHE.stat().st_mtime > max_age_h * 3600):
        raw = urllib.request.urlopen(_SCRIP_MASTER_URL, timeout=120).read() \
            .decode("utf-8", "replace")
        lines = raw.splitlines()
        # First column is SEM_EXM_EXCH_ID (see resolve_mcx_ids' MCX, filter);
        # keep only NSE rows that name a stock future or a cash equity.
        keep = [lines[0]] + [
            ln for ln in lines[1:]
            if ln.startswith("NSE,") and ("FUTSTK" in ln or "EQUITY" in ln)]
        _FNO_MASTER_CACHE.write_text("\n".join(keep), encoding="utf-8")

    rows = list(csv.DictReader(io.StringIO(
        _FNO_MASTER_CACHE.read_text(encoding="utf-8"))))
    universe = parse_fno_universe(rows, today=today)
    if store is not None and hasattr(store, "upsert_fno_universe"):
        as_of = today or datetime.now(IST).date()
        store.upsert_fno_universe(as_of, universe)
    return universe


# ---------------------------------------------------------------------------
# SDK client (lazy import so parsing/tests don't require dhanhq to be present)
# ---------------------------------------------------------------------------

def resolve_credentials() -> tuple[str, str]:
    """Return (client_id, access_token) from env (DHAN_CLIENT_ID/
    DHAN_ACCESS_TOKEN), a DHAN_CONFIG_PATH json file, or the server's managed
    24h token (SQLite). Raises if none are found."""
    client_id = os.environ.get("DHAN_CLIENT_ID")
    access_token = os.environ.get("DHAN_ACCESS_TOKEN")
    cfg_path = os.environ.get("DHAN_CONFIG_PATH")
    if (not client_id or not access_token) and cfg_path and os.path.exists(cfg_path):
        import json
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        client_id = client_id or cfg.get("client_id")
        access_token = access_token or cfg.get("access_token")
    if not access_token:
        # Fall back to the server's managed 24h token (SQLite) so callers share
        # the exact credential the running app uses, not a duplicate.
        try:
            from app.core import token_manager
            access_token = token_manager.get_access_token()
            client_id = client_id or token_manager.CLIENT_ID
        except Exception:
            pass
    if not client_id or not access_token:
        raise RuntimeError(
            "Dhan credentials not found. Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN "
            "(or DHAN_CONFIG_PATH to a json config).")
    return client_id, access_token


def get_dhan_context():
    """DhanContext for SDK objects that need it directly (e.g. MarketFeed)."""
    from dhanhq import DhanContext  # lazy: only needed for live use
    return DhanContext(*resolve_credentials())


def get_client():
    """Build a dhanhq client from resolved credentials. Raises if missing."""
    from dhanhq import dhanhq  # lazy: only needed for live fetch
    return dhanhq(get_dhan_context())


def _unwrap(response: dict) -> dict:
    """Return the `data` payload of a successful SDK response, else raise."""
    if not isinstance(response, dict):
        raise RuntimeError(f"unexpected SDK response: {response!r}")
    if response.get("status") != "success":
        raise RuntimeError(response.get("remarks") or "Dhan SDK call failed")
    return response.get("data") or {}


def fetch_intraday(client, security_id: int, segment: str, instrument: str,
                   interval: int, from_dt: str, to_dt: str, oi: bool = False) -> dict:
    """Underlying candles. Returns the unwrapped data dict of parallel arrays."""
    resp = client.intraday_minute_data(
        security_id=str(security_id), exchange_segment=segment,
        instrument_type=instrument, from_date=from_dt, to_date=to_dt,
        interval=int(interval), oi=oi)
    return _unwrap(resp)


def fetch_expired_option(client, security_id: int, fno_segment: str, instrument: str,
                         interval: int, expiry_flag: str, expiry_code: int,
                         strike: str, option_type: str,
                         from_d: str, to_d: str) -> dict:
    """Expired option candles for one ATM-relative strike/side. `strike` is
    'ATM', 'ATM+2', 'ATM-3'. `expiry_code` is 1-indexed (1 = nearest expiry of
    `expiry_flag`, 2 = next, ...); 0 is rejected by the API. Returns the
    unwrapped data dict; the live SDK nests arrays under data['data']['ce'|'pe']."""
    resp = client.expired_options_data(
        security_id=security_id, exchange_segment=fno_segment,
        instrument_type=instrument, expiry_flag=expiry_flag,
        expiry_code=expiry_code, strike=strike, drv_option_type=option_type,
        required_data=["open", "high", "low", "close", "volume", "oi", "iv",
                       "strike", "spot"],
        from_date=from_d, to_date=to_d, interval=int(interval))
    return _unwrap(resp)


def fetch_quotes(client, securities: dict) -> dict:
    """Batched market quote for many instruments in ONE call (Tier-1 scanner).
    `securities` is {exchange_segment: [security_id, ...]}, e.g.
    {"NSE_FNO": [46376, 46801], "NSE_EQ": [2885]}. Returns {segment:
    {sid_str: node}} — descends the SDK's double-nested payload. Each node
    carries last_price, volume, oi and an `ohlc` sub-dict (open/high/low/close,
    where close is the PREVIOUS session's close). Dhan caps instruments per
    quote call (~1000); the caller chunks and stays ~1 req/s."""
    resp = client.quote_data(securities)
    data = _unwrap(resp)
    if isinstance(data.get("data"), dict):
        data = data["data"]
    return data


def fetch_option_chain(client, underlying_scrip: int, underlying_seg: str, expiry: str) -> dict:
    """Live chain with greeks. Rate limit: 1 unique request per 3 seconds.
    Returns the unwrapped data dict (spot at data['last_price'], strikes under
    data['oc'])."""
    resp = client.option_chain(under_security_id=underlying_scrip,
                               under_exchange_segment=underlying_seg, expiry=expiry)
    return _unwrap(resp)


# ---------------------------------------------------------------------------
# Parsing: unwrapped data dict -> rows ready for the store. Pure, testable.
# ---------------------------------------------------------------------------

def _epoch_to_ist(epoch) -> datetime:
    """Dhan epoch seconds -> naive IST datetime (the store's convention)."""
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).astimezone(IST).replace(tzinfo=None)


def _at(d: dict, key: str, i: int):
    arr = d.get(key)
    return arr[i] if arr and i < len(arr) else None


def parse_intraday_rows(underlying: str, data: dict) -> list[tuple]:
    """data: parallel arrays {timestamp, open, high, low, close, volume,
    open_interest?}. -> rows for underlying_bars
    (underlying, ts, open, high, low, close, volume, oi)."""
    ts_arr = data.get("timestamp") or []
    rows = []
    for i in range(len(ts_arr)):
        rows.append((
            underlying, _epoch_to_ist(ts_arr[i]),
            _at(data, "open", i), _at(data, "high", i),
            _at(data, "low", i), _at(data, "close", i),
            _at(data, "volume", i) or 0, _at(data, "open_interest", i) or 0))
    return rows


_SIDE_FOR = {"CALL": "ce", "PUT": "pe"}


def parse_expired_option_rows(underlying: str, strike_offset: int, option_type: str,
                              data: dict, expiry_kind: str = "WEEKLY",
                              expiry_offset: int = 0) -> list[tuple]:
    """data: {'ce': {...arrays}, 'pe': {...arrays}} — or the live SDK's extra
    wrapper {'data': {'ce': ..., 'pe': ...}}, which we descend into. Only the
    side matching `option_type` is populated (CALL->ce, PUT->pe); the other is
    None. Each side has parallel arrays {timestamp, open, high, low, close,
    volume, oi, iv, strike, spot}. -> rows for option_bars (underlying, ts,
    expiry_kind, expiry_offset, strike_offset, option_type, strike, expiry,
    o, h, l, c, volume, oi, iv).

    `expiry` (absolute date) is not returned by the rolling API, so it is
    stored NULL; strategies key on the ATM-relative offsets, not the date."""
    # The live expired-options payload nests ce/pe under a second 'data' key;
    # saved fixtures may hold {ce,pe} directly. Handle both.
    if isinstance(data.get("data"), dict) and ("ce" in data["data"] or "pe" in data["data"]):
        data = data["data"]
    side = data.get(_SIDE_FOR[option_type]) or {}
    ts_arr = side.get("timestamp") or []
    rows = []
    for i in range(len(ts_arr)):
        rows.append((
            underlying, _epoch_to_ist(ts_arr[i]), expiry_kind, expiry_offset,
            strike_offset, option_type,
            _at(side, "strike", i), None,  # absolute strike (if present), expiry=NULL
            _at(side, "open", i), _at(side, "high", i),
            _at(side, "low", i), _at(side, "close", i),
            _at(side, "volume", i) or 0, _at(side, "oi", i) or 0,
            _at(side, "iv", i)))
    return rows


# ---------------------------------------------------------------------------
# Upserts (idempotent; INSERT OR REPLACE on the tables' primary keys)
# ---------------------------------------------------------------------------

def upsert_underlying_rows(store, rows: list[tuple]) -> int:
    if rows:
        store.con.executemany(
            "INSERT OR REPLACE INTO underlying_bars VALUES (?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def upsert_option_rows(store, rows: list[tuple]) -> int:
    if rows:
        store.con.executemany(
            "INSERT OR REPLACE INTO option_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


# ---------------------------------------------------------------------------
# Backfill orchestration (chunked, rate-limited, idempotent)
# ---------------------------------------------------------------------------

def backfill(underlying: str, start: date, end: date,
             strike_offsets=range(-5, 6), interval: int = 1,
             expiry_flag: str = "WEEK", expiry_offset: int = 0,
             client=None, store=None, progress=None, skip_existing: bool = True) -> None:
    """Backfill underlying candles + ATM-relative expired-option candles.

    `expiry_offset` is the store's 0-indexed expiry (0 = nearest/current);
    it maps to the API's 1-indexed expiry_code (offset + 1). Pass an existing
    `store` to reuse a connection (the server does this to avoid a second
    DuckDB writer). `progress(msg)` is called per chunk for UI streaming.
    `skip_existing` (default) skips chunks already present so an interrupted
    backfill RESUMES on re-run instead of re-fetching everything."""
    if store is None:
        from app.data.store import DataStore
        store = DataStore()
    cfg = UNDERLYINGS[underlying]
    client = client or get_client()

    def _log(msg):
        print(msg, flush=True)
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    from app.core import registry  # lazy: chunk ledger lives in SQLite

    def _fetch(fn, *a, tries: int = 5, **kw):
        """Retry a single-chunk fetch through transient network/API blips
        (connection resets, timeouts) so one hiccup doesn't abort a multi-hour
        backfill. Exponential backoff; re-raises after the last try (the chunk
        is left un-ledgered so a resume retries it)."""
        for i in range(tries):
            try:
                return fn(*a, **kw)
            except Exception as e:
                if i == tries - 1:
                    raise
                wait = min(2 ** i, 20)
                _log(f"  fetch error ({e!r}); retry {i + 1}/{tries - 1} in {wait}s")
                time.sleep(wait)

    kind = "WEEKLY" if expiry_flag == "WEEK" else "MONTHLY"

    # Resume is driven by a LEDGER of chunks that were successfully upserted —
    # NOT by "are there any rows in this range". Row-existence is unsafe: a
    # partially-written chunk (interrupted, or clobbered by a concurrent run)
    # looks 'present' and would be skipped forever, leaving silent data gaps.

    # 1) underlying candles, <=89-day chunks
    cur = start
    while cur < end:
        chunk_end = min(cur + timedelta(days=89), end)
        key = registry.chunk_key(underlying, "spot", interval, cur, chunk_end)
        if skip_existing and registry.is_chunk_done(key):
            _log(f"underlying {cur} -> {chunk_end}: done, skip")
        else:
            data = _fetch(fetch_intraday, client, cfg["security_id"], cfg["segment"],
                          "INDEX", interval, f"{cur} 09:15:00", f"{chunk_end} 15:30:00", oi=False)
            n = upsert_underlying_rows(store, parse_intraday_rows(underlying, data))
            registry.mark_chunk_done(key)      # only AFTER a successful upsert
            _log(f"underlying {cur} -> {chunk_end}: {n} bars")
            time.sleep(0.3)
        cur = chunk_end

    # 2) expired options, <=29-day chunks per (strike_offset, option_type)
    for off in strike_offsets:
        strike = "ATM" if off == 0 else f"ATM{'+' if off > 0 else ''}{off}"
        for opt in ("CALL", "PUT"):
            cur = start
            while cur < end:
                chunk_end = min(cur + timedelta(days=29), end)
                key = registry.chunk_key(underlying, "opt", interval, cur, chunk_end,
                                         off=off, opt=opt, expiry_kind=kind,
                                         expiry_offset=expiry_offset)
                if skip_existing and registry.is_chunk_done(key):
                    _log(f"{strike} {opt} {cur} -> {chunk_end}: done, skip")
                    cur = chunk_end
                    continue
                data = _fetch(
                    fetch_expired_option,
                    client, cfg["security_id"], cfg["fno_segment"], cfg["instrument"],
                    interval, expiry_flag, expiry_offset + 1, strike, opt, str(cur), str(chunk_end))
                n = upsert_option_rows(store, parse_expired_option_rows(
                    underlying, off, opt, data, expiry_offset=expiry_offset, expiry_kind=kind))
                registry.mark_chunk_done(key)  # only AFTER a successful upsert
                _log(f"{strike} {opt} {cur} -> {chunk_end}: {n} bars")
                cur = chunk_end
                time.sleep(0.3)  # stay well under rate limits


if __name__ == "__main__":
    import sys
    _, cmd, und, s, e = sys.argv
    assert cmd == "backfill", "usage: python -m app.data.dhan_client backfill NIFTY 2024-01-01 2025-01-01"
    backfill(und, date.fromisoformat(s), date.fromisoformat(e))
