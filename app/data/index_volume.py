"""Retroactive index VOLUME backfill — pure planning + row building.

Indexes carry no volume of their own, so `underlying_bars` holds volume=0 for
every NIFTY/BANKNIFTY bar recorded before the index-futures companion was
switched on (2026-07-27). Any indicator that consumes volume — `vwap()` above
all — silently degraded to an unweighted average over that whole history: it
returned a number, the number just wasn't VWAP.

The companion fixes it going FORWARD by folding the front-month future's
volume/OI into the live spot candle. This module does the same thing
BACKWARD, from Dhan's intraday historical for that same future, so the
recorded sessions become usable for volume-aware backtests instead of only
future ones.

Two rules make it safe:

  1. PRICE IS NEVER TOUCHED. Only volume/OI are filled, and only into bars
     that already exist. The index's own OHLC is the real spot print and stays
     authoritative — mirrors MarketHub._on_companion, which likewise adds
     volume without touching price.

  2. THE CONTRACT MUST BE THE ONE THAT ACTUALLY TRADED THAT DAY. Index futures
     roll monthly, and Dhan's scrip master lists only CURRENTLY-LISTED
     contracts — once a contract expires it disappears. So resolving an old
     date against today's master happily returns the *next* contract, which
     was listed then but was NOT the front month, and whose volume is a
     fraction of the real figure. That is wrong data that looks entirely
     plausible, so `front_month_for` refuses rather than guesses: a resolved
     expiry more than `max_ahead_days` past the target date returns None and
     the caller reports the date as unbackfillable.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

# A front-month index future never expires more than ~1 month out. Anything
# further means the true front month has been delisted and we are looking at
# the next contract — refuse it. 45 days leaves room for a long month plus
# the roll, without being loose enough to admit the month after.
MAX_AHEAD_DAYS = 45


def front_month_for(master_rows: list, name: str, day: date,
                    max_ahead_days: int = MAX_AHEAD_DAYS) -> dict | None:
    """The FUTIDX contract that was front-month for `name` on `day`.

    Returns None when it cannot be established — most often because that
    contract has since expired and dropped out of the scrip master. Returning
    None is the whole point: see rule 2 in the module docstring.
    """
    from app.data.dhan_client import parse_index_futures

    got = parse_index_futures(master_rows, (name,), day.isoformat())
    fut = got.get(name)
    if not fut:
        return None
    try:
        expiry = datetime.fromisoformat(fut["expiry"][:10]).date()
    except (ValueError, KeyError, TypeError):
        return None
    if not (day <= expiry <= day + timedelta(days=max_ahead_days)):
        return None                      # delisted front month -> refuse
    return fut


def contract_spans(master_rows: list, name: str, days: list,
                   max_ahead_days: int = MAX_AHEAD_DAYS) -> tuple[list, list]:
    """Group `days` into (security_id, expiry, first_day, last_day) spans.

    Consecutive days served by the same contract become ONE fetch — Dhan's
    intraday endpoint takes a date range, and a monthly contract usually
    covers the whole window in a single call. Returns (spans, unresolved),
    where `unresolved` lists the days no contract could be established for.
    """
    spans, unresolved, cur = [], [], None
    for day in sorted(days):
        fut = front_month_for(master_rows, name, day, max_ahead_days)
        if fut is None:
            unresolved.append(day)
            cur = None                   # a hole breaks the span
            continue
        sid = fut["security_id"]
        if cur and cur["security_id"] == sid:
            cur["last_day"] = day
        else:
            cur = {"security_id": sid, "expiry": fut["expiry"],
                   "first_day": day, "last_day": day}
            spans.append(cur)
    return spans, unresolved


def volume_rows_from_intraday(underlying: str, data: dict, days: set) -> list:
    """Futures intraday arrays -> [(underlying, ts, volume, oi)] update rows.

    Only bars whose DATE is in `days` are kept: a span fetch may span a
    weekend or a session we never recorded, and those must not be written.
    Bars with no volume are dropped — writing 0 over 0 is a no-op that would
    only inflate the reported count.
    """
    from app.data.dhan_client import _at, _epoch_to_ist

    ts_arr = data.get("timestamp") or []
    rows = []
    for i in range(len(ts_arr)):
        ts = _epoch_to_ist(ts_arr[i])
        if ts.date() not in days:
            continue
        vol = _at(data, "volume", i) or 0
        if not vol:
            continue
        rows.append((underlying, ts, float(vol),
                     float(_at(data, "open_interest", i) or 0)))
    return rows
