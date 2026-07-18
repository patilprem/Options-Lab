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
