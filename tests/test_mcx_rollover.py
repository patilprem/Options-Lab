"""MCX contract-rollover coverage — CRUDEOIL *and* GOLD.

A commodity chain hangs off the CURRENT futures contract, whose security id
rolls at expiry. resolve_mcx_ids() must re-point every MCX_DYNAMIC name (not
just crude) to its nearest non-expired FUTCOM, and a changed id must be
detectable so the feed can force-resubscribe (else that underlying's live
recording silently freezes on the dead contract — the CRUDEOIL-Fri-17 gap).

Fully offline: a temp scrip-master CSV replaces the network download.
"""

from __future__ import annotations

import pytest

from app.data import dhan_client as dc

_HEADER = "SEM_INSTRUMENT_NAME,SEM_TRADING_SYMBOL,SEM_EXPIRY_DATE,SEM_SMST_SECURITY_ID"


def _write_master(path, rows):
    path.write_text("\n".join([_HEADER, *rows]), encoding="utf-8")


@pytest.fixture
def master(tmp_path, monkeypatch):
    """Point resolve_mcx_ids at a temp cache and restore the mutated globals."""
    cache = tmp_path / "scrip_master_mcx.csv"
    monkeypatch.setattr(dc, "_MASTER_CACHE", cache)
    saved = {n: dc.UNDERLYINGS.get(n) for n in dc.MCX_DYNAMIC}
    yield cache
    for n, v in saved.items():                 # don't leak into other tests
        if v is None:
            dc.UNDERLYINGS.pop(n, None)
        else:
            dc.UNDERLYINGS[n] = v


# Far-future expiries keep the test stable regardless of the real clock.
_ROWS_V1 = [
    "FUTCOM,CRUDEOIL-01JAN2000-FUT,2000-01-01,900",    # expired  -> excluded
    "FUTCOM,CRUDEOIL-19AUG2099-FUT,2099-08-19,1001",   # crude nearest -> picked
    "FUTCOM,GOLD-05AUG2099-FUT,2099-08-05,2001",       # gold nearest  -> picked
    "FUTCOM,GOLD-05OCT2099-FUT,2099-10-05,2002",       # gold far      -> not picked
    "FUTCOM,GOLDM-31AUG2099-FUT,2099-08-31,3001",      # MINI -> excluded (GOLD- prefix)
    "OPTFUT,GOLD-05AUG2099-CE,2099-08-05,4001",        # option -> excluded (not FUTCOM)
]


def test_resolve_picks_nearest_future_for_both_crude_and_gold(master):
    _write_master(master, _ROWS_V1)

    ids = dc.resolve_mcx_ids()

    assert ids == {"CRUDEOIL": 1001, "GOLD": 2001}     # gold handled just like crude
    g = dc.UNDERLYINGS["GOLD"]
    assert g["security_id"] == 2001 and g["expiry"] == "2099-08-05"
    assert g["segment"] == "MCX_COMM" and g["instrument"] == "OPTFUT"
    # the mini (GOLDM) and the option row must never be mistaken for the future
    assert dc.UNDERLYINGS["GOLD"]["security_id"] not in (3001, 4001)


def test_gold_rollover_id_change_is_detected(master):
    """The exact comparison _market_recorder uses (main.py) must flag GOLD
    when its futures id rolls — the trigger for hub.resubscribe()."""
    _write_master(master, _ROWS_V1)
    dc.resolve_mcx_ids()                                # seed: GOLD -> 2001

    old_ids = {n: dc.UNDERLYINGS.get(n, {}).get("security_id")
               for n in dc.MCX_DYNAMIC}

    # gold rolls: the Aug contract is gone, Oct (id 2002) is now nearest
    _write_master(master, [r for r in _ROWS_V1 if "GOLD-05AUG2099" not in r])
    ids = dc.resolve_mcx_ids()

    rolled = [n for n, sid in ids.items()
              if old_ids.get(n) is not None and old_ids[n] != sid]
    assert rolled == ["GOLD"]                           # -> feed resubscribes
    assert ids["GOLD"] == 2002 and ids["CRUDEOIL"] == 1001  # crude unchanged


# --- MCX names whose security id resolves at RUNTIME ------------------------

def test_enable_chain_refreshes_once_the_runtime_id_arrives(monkeypatch):
    """2026-08-04 09:21: underlying_bars[GOLD] dead all morning while
    chain_snapshots[GOLD] recorded fine — ticks absent, REST chain present.

    An MCX security id is resolved at runtime, and _instruments() skips any
    name missing from UNDERLYINGS. If the recorder's FIRST enable_chain("GOLD")
    lands before that resolve succeeds, the old `first_time` guard skipped the
    refresh — and every later call had first_time=False, so the id arriving a
    minute later could never trigger one. GOLD stayed off the socket for the
    session while the chain poller, which reads UNDERLYINGS at poll time, kept
    working."""
    from app.data.dhan_client import UNDERLYINGS
    from app.engines.paper import MarketHub

    hub = MarketHub.__new__(MarketHub)
    hub._wanted, hub._chain_only, hub._builders = {}, set(), {}
    hub._chain_subscribed = set()
    hub._started = True
    refreshes = []
    hub._livefeed = type("F", (), {"refresh": lambda self: refreshes.append(1)})()

    # first call: id not resolved yet -> nothing to subscribe, no refresh
    hub.enable_chain("GOLD")
    assert refreshes == []
    assert "GOLD" in hub._chain_only

    # resolve_mcx_ids lands; the very next recorder cycle must put it on the wire
    monkeypatch.setitem(UNDERLYINGS, "GOLD",
                        {"security_id": 466583, "segment": "MCX_COMM"})
    hub.enable_chain("GOLD")
    assert refreshes == [1], "the id arrived and nothing resubscribed"

    # ...and not once per cycle thereafter
    hub.enable_chain("GOLD")
    hub.enable_chain("GOLD")
    assert refreshes == [1]


def test_enable_chain_still_refreshes_a_name_that_was_ready_immediately(monkeypatch):
    from app.data.dhan_client import UNDERLYINGS
    from app.engines.paper import MarketHub

    hub = MarketHub.__new__(MarketHub)
    hub._wanted, hub._chain_only, hub._builders = {}, set(), {}
    hub._chain_subscribed = set()
    hub._started = True
    refreshes = []
    hub._livefeed = type("F", (), {"refresh": lambda self: refreshes.append(1)})()

    monkeypatch.setitem(UNDERLYINGS, "CRUDEOIL",
                        {"security_id": 560977, "segment": "MCX_COMM"})
    hub.enable_chain("CRUDEOIL")
    assert refreshes == [1]
