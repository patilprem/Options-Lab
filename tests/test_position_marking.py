"""Floating-strike MTM regression (paper/live) + derived expiry calendar.

The chain cache is keyed ATM-relative and re-anchors every poll. Marking an
open position through its leg key silently re-prices it to a DIFFERENT
contract once spot moves a strike step — and a live exit would even route the
order to the wrong security_id. quote_position() must return the contract
actually held."""

from datetime import date, datetime

from app.core.contract import (Action, ExpiryKind, LegSpec, OptionQuote,
                               OptionType, Position)
from app.data.store import DataStore
from app.engines.paper import MarketHub


def _quote(strike, ltp, sid, ts, expiry=date(2026, 7, 14)):
    return OptionQuote(ts, "NIFTY", expiry, strike, OptionType.CALL,
                       ltp=ltp, security_id=sid)


def _position(strike):
    leg = LegSpec(OptionType.CALL, Action.BUY, 0, ExpiryKind.WEEKLY, 0, 1)
    return Position(id="t1", leg=leg, underlying="NIFTY",
                    expiry=date(2026, 7, 14), strike=strike, qty=75,
                    entry_price=150.0, entry_ts=datetime(2026, 7, 10, 10, 0),
                    mtm_price=150.0)


def test_quote_position_tracks_fixed_strike_not_floating_atm(tmp_path):
    hub = MarketHub(DataStore(tmp_path / "m.duckdb"))
    ts = datetime(2026, 7, 10, 11, 0)
    # spot rallied one step: offset 0 now points at 24100, the held 24050
    # contract is filed under offset -1
    hub._chain_cache["NIFTY"] = {
        ("WEEKLY", 0, 0, "CALL"): _quote(24100.0, 95.0, "sidB", ts),
        ("WEEKLY", 0, -1, "CALL"): _quote(24050.0, 210.0, "sidA", ts),
    }
    pos = _position(24050.0)

    # leg-key quote returns the WRONG (current-ATM) contract — the old bug
    wrong = hub.quote("NIFTY", ts, pos.leg)
    assert wrong.strike == 24100.0

    # quote_position returns the contract actually held
    q = hub.quote_position("NIFTY", ts, pos)
    assert q.strike == 24050.0
    assert q.ltp == 210.0
    assert q.security_id == "sidA"      # live exits route to the RIGHT contract


def test_quote_position_falls_back_to_store_by_strike(tmp_path):
    st = DataStore(tmp_path / "m.duckdb")
    st.con.execute(
        "INSERT INTO option_bars VALUES ('NIFTY', ?, 'WEEKLY',0,-2,'CALL',"
        "24050,NULL,200,200,200,205,0,0,12)", [datetime(2026, 7, 10, 10, 55)])
    hub = MarketHub(st)                       # empty chain cache (dev/backtest)
    q = hub.quote_position("NIFTY", datetime(2026, 7, 10, 11, 0),
                           _position(24050.0))
    assert q is not None and q.ltp == 205


def test_chain_fingerprint_skips_frozen_chain(tmp_path):
    """Holiday guard: a frozen (unchanged) chain must not be re-persisted —
    Dhan serves the last session's chain on closed days with fresh-looking
    timestamps, which previously produced junk rows and fake learning days."""
    hub = MarketHub(DataStore(tmp_path / "m.duckdb"))
    ts = datetime(2026, 8, 15, 11, 0)          # a weekday holiday
    hub._chain_cache["NIFTY"] = {
        ("WEEKLY", 0, 0, "CALL"): _quote(24100.0, 95.0, "s1", ts),
        ("WEEKLY", 0, 0, "PUT"): _quote(24100.0, 88.0, "s2", ts),
    }
    hub._chain_spot["NIFTY"] = 24101.5

    assert hub.chain_changed(["NIFTY"]) == ["NIFTY"]   # first sight: record
    hub.mark_chain_persisted(["NIFTY"])
    assert hub.chain_changed(["NIFTY"]) == []          # frozen: skip

    # market actually moves -> record again
    hub._chain_cache["NIFTY"][("WEEKLY", 0, 0, "CALL")] = _quote(24100.0, 97.5, "s1", ts)
    assert hub.chain_changed(["NIFTY"]) == ["NIFTY"]
    # no cache at all (never polled) -> never reported as changed
    assert hub.chain_changed(["CRUDEOIL"]) == []


def test_expiry_calendar_derived_and_filled(tmp_path):
    """Expiry days are detected from the ATM-straddle EOD collapse and used to
    fill the NULL expiry column (offset-aware, never expiry < bar date)."""
    from app.data import expiries
    st = DataStore(tmp_path / "m.duckdb")
    rows = []
    # 10 weekdays; every 5th day the straddle collapses (expiry)
    days = [date(2026, 6, d) for d in (1, 2, 3, 4, 5, 8, 9, 10, 11, 12)]
    for i, d in enumerate(days):
        eod = 5.0 if (i + 1) % 5 == 0 else 100.0     # collapse on 5th/10th
        for otype in ("CALL", "PUT"):
            rows.append(("NIFTY", datetime(d.year, d.month, d.day, 15, 25),
                         "WEEKLY", 0, 0, otype, 24000.0, None,
                         eod, eod, eod, eod, 0, 0, 12.0))
    st.con.executemany(
        "INSERT INTO option_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    detected = expiries.detect_weekly_expiries(st, "NIFTY")
    assert detected == [date(2026, 6, 5), date(2026, 6, 12)]

    res = expiries.rebuild(st, "NIFTY")
    assert res["expiries"] == 2
    # bars before/on 6/5 -> expiry 6/5; bars 6/8..6/12 -> 6/12
    got = dict(st._q(
        """SELECT CAST(ts AS DATE), expiry FROM option_bars
           WHERE option_type='CALL' ORDER BY ts"""))
    assert got[date(2026, 6, 3)] == date(2026, 6, 5)
    assert got[date(2026, 6, 5)] == date(2026, 6, 5)
    assert got[date(2026, 6, 9)] == date(2026, 6, 12)
    bad = st._q1("""SELECT count(*) FROM option_bars
                    WHERE expiry IS NOT NULL AND expiry < CAST(ts AS DATE)""")[0]
    assert bad == 0
