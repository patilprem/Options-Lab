"""M5: real margin (API + cache + fallback) and the calibration factor.
Offline — a stub client stands in for the Dhan margin API."""

from __future__ import annotations

import pytest

from app.core import registry
from app.core.contract import Action
from app.engines import fills as F
from app.engines import margin as M


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "m.db")
    registry.init_db()
    M.clear_cache()
    return tmp_path


class StubClient:
    def __init__(self, per_leg=50000.0, fail=False):
        self.per_leg, self.fail, self.calls = per_leg, fail, 0

    def margin_calculator(self, **kw):
        self.calls += 1
        if self.fail:
            return {"status": "failure", "remarks": "DH-905"}
        return {"status": "success", "data": {"totalMargin": self.per_leg}}


def _legs(with_ids=True):
    return [
        {"security_id": "111" if with_ids else None, "action": Action.SELL,
         "qty_units": 75, "price": 145.0},
        {"security_id": "222" if with_ids else None, "action": Action.SELL,
         "qty_units": 75, "price": 124.0},
    ]


# --- estimate_margin factor -------------------------------------------------

def test_estimate_margin_factor_scales_short_term():
    legs = [(100.0, Action.SELL, 75)]
    base = F.estimate_margin(legs, 20000, 75)
    assert F.estimate_margin(legs, 20000, 75, factor=1.5) == pytest.approx(base * 1.5)
    # long premium is exact and unaffected by the factor
    buy = [(100.0, Action.BUY, 75)]
    assert F.estimate_margin(buy, 20000, 75, factor=5) == 100.0 * 75


# --- real_margin ------------------------------------------------------------

def test_real_margin_sums_api_legs_and_caches(db):
    client = StubClient(per_leg=60000.0)
    got = M.real_margin(_legs(), spot=24000, lot_size=75, underlying="NIFTY", client=client)
    assert got == 120000.0 and client.calls == 2
    # identical structure -> served from cache, no further API calls
    again = M.real_margin(_legs(), spot=24000, lot_size=75, underlying="NIFTY", client=client)
    assert again == 120000.0 and client.calls == 2


def test_real_margin_falls_back_without_client(db):
    est = M.real_margin(_legs(), spot=24000, lot_size=75, underlying="NIFTY", client=None)
    expected = F.estimate_margin([(145.0, Action.SELL, 75), (124.0, Action.SELL, 75)],
                                 24000, 75)  # factor 1.0 (unset)
    assert est == expected


def test_real_margin_falls_back_when_legs_lack_security_id(db):
    client = StubClient()
    est = M.real_margin(_legs(with_ids=False), spot=24000, lot_size=75,
                        underlying="NIFTY", client=client)
    assert client.calls == 0  # never queried the API
    assert est == F.estimate_margin([(145.0, Action.SELL, 75), (124.0, Action.SELL, 75)],
                                    24000, 75)


def test_real_margin_falls_back_on_api_failure(db):
    client = StubClient(fail=True)
    est = M.real_margin(_legs(), spot=24000, lot_size=75, underlying="NIFTY", client=client)
    assert est == F.estimate_margin([(145.0, Action.SELL, 75), (124.0, Action.SELL, 75)],
                                    24000, 75)


# --- calibration factor -----------------------------------------------------

def test_underlying_factor_from_settings(db):
    assert M.underlying_factor("NIFTY") == 1.0            # unset -> 1.0
    registry.set_setting("margin_factor:NIFTY", "1.35")
    assert M.underlying_factor("NIFTY") == 1.35
    registry.set_setting("margin_factor:NIFTY", "bad")    # garbage -> 1.0
    assert M.underlying_factor("NIFTY") == 1.0


def test_real_margin_fallback_uses_calibrated_factor(db):
    registry.set_setting("margin_factor:NIFTY", "2.0")
    est = M.real_margin(_legs(), spot=24000, lot_size=75, underlying="NIFTY", client=None)
    expected = F.estimate_margin([(145.0, Action.SELL, 75), (124.0, Action.SELL, 75)],
                                 24000, 75, factor=2.0)
    assert est == expected
