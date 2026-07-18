"""Rename / edit-code / delete a strategy — offline, registry + API layer.

Edit-code re-validates like POST /strategies and resets to VALIDATED;
delete removes the row + its history. Both are blocked while the
strategy is RUNNING/DEPLOYED_PAUSED (must stop first).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api.strategies import (RenameReq, UpdateCodeReq, delete_strategy,
                                rename_strategy, update_code)
from app.core import registry
from app.core.registry import State

EXAMPLE = (Path(__file__).resolve().parents[1] / "examples" / "ema_atr_trend.py").read_text()
BROKEN = "class NotAStrategy:\n    pass\n"


@pytest.fixture
def rec(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "test.db")
    registry.init_db()
    r = registry.create("Original name", EXAMPLE)
    registry.transition(r.id, State.VALIDATED)
    return registry.get(r.id)


# --- rename ------------------------------------------------------------

def test_rename_updates_name(rec):
    out = rename_strategy(rec.id, RenameReq(name="  Renamed strategy  "))
    assert out["name"] == "Renamed strategy"
    assert registry.get(rec.id).name == "Renamed strategy"


def test_rename_rejects_empty_name(rec):
    with pytest.raises(HTTPException) as exc:
        rename_strategy(rec.id, RenameReq(name="   "))
    assert exc.value.status_code == 400


def test_rename_unknown_id_404(rec):
    with pytest.raises(HTTPException) as exc:
        rename_strategy("nope", RenameReq(name="x"))
    assert exc.value.status_code == 404


# --- edit code -----------------------------------------------------------

def test_update_code_revalidates_and_resets_state(rec):
    out = update_code(rec.id, UpdateCodeReq(code=EXAMPLE))
    assert out["state"] == "VALIDATED"
    stored = registry.get(rec.id)
    assert stored.state == State.VALIDATED
    assert stored.code == EXAMPLE


def test_update_code_rejects_invalid_code_without_persisting(rec):
    original_code = rec.code
    with pytest.raises(HTTPException) as exc:
        update_code(rec.id, UpdateCodeReq(code=BROKEN))
    assert exc.value.status_code == 422
    stored = registry.get(rec.id)
    assert stored.code == original_code   # rejected edit never touches the row
    assert stored.state == State.VALIDATED


def test_update_code_blocked_while_running(rec):
    registry.transition(rec.id, State.DEPLOYED_PAUSED)
    registry.transition(rec.id, State.RUNNING)
    with pytest.raises(HTTPException) as exc:
        update_code(rec.id, UpdateCodeReq(code=EXAMPLE))
    assert exc.value.status_code == 400
    assert registry.get(rec.id).state == State.RUNNING


# --- delete --------------------------------------------------------------

def test_delete_removes_strategy_and_history(rec):
    registry.save_backtest(rec.id, "bt-1", "2026-01-01", "2026-01-02", {"ok": True})
    registry.record_event("info", "lifecycle", "hello", rec.id)
    registry.set_params(rec.id, {"x": 1})

    delete_strategy(rec.id)

    assert registry.get(rec.id) is None
    assert registry.backtests(rec.id) == []
    assert registry.get_params(rec.id) == {}


def test_delete_blocked_while_deployed(rec):
    registry.transition(rec.id, State.DEPLOYED_PAUSED)
    with pytest.raises(HTTPException) as exc:
        delete_strategy(rec.id)
    assert exc.value.status_code == 400
    assert registry.get(rec.id) is not None


def test_delete_unknown_id_404(rec):
    with pytest.raises(HTTPException) as exc:
        delete_strategy("nope")
    assert exc.value.status_code == 404
