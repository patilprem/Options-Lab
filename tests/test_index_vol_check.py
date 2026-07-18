"""
Index-futures volume self-check orchestration (step 4 residual, option 1)
=========================================================================
Offline: no live socket, no SDK. The three sub-checks and the socket observe
are monkeypatched; we pin the aggregation + the report/persist behavior that
runs on the VPS and pushes the verdict.
"""

from __future__ import annotations

import pytest

from app.core import registry
from app.engines import index_vol_check as ivc


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "t.db")
    registry.init_db()
    return tmp_path


def test_run_full_check_all_pass(monkeypatch):
    monkeypatch.setattr(ivc, "check_resolution",
                        lambda syms: ({"NIFTY": {"security_id": 1}}, ["r"]))
    monkeypatch.setattr(ivc, "check_constants", lambda: (True, ["c"]))
    monkeypatch.setattr(ivc, "_observe_sync", lambda r, s, sec: (True, ["o"]))
    res = ivc.run_full_check(["NIFTY"], seconds=1)
    assert res["passed"] is True and res["verdict"] == "PASS"
    assert res["lines"] == ["r", "c", "o"]


def test_run_full_check_fails_if_no_resolution(monkeypatch):
    monkeypatch.setattr(ivc, "check_resolution", lambda syms: ({}, ["empty"]))
    monkeypatch.setattr(ivc, "check_constants", lambda: (True, ["c"]))
    # observe must NOT be called when nothing resolved
    monkeypatch.setattr(ivc, "_observe_sync",
                        lambda *a: (_ for _ in ()).throw(AssertionError("called")))
    res = ivc.run_full_check(["NIFTY"], seconds=1)
    assert res["passed"] is False
    assert any("SKIPPED" in ln for ln in res["lines"])


def test_run_full_check_fails_if_constants_mismatch(monkeypatch):
    monkeypatch.setattr(ivc, "check_resolution",
                        lambda syms: ({"NIFTY": {"security_id": 1}}, ["r"]))
    monkeypatch.setattr(ivc, "check_constants", lambda: (False, ["MISMATCH"]))
    monkeypatch.setattr(ivc, "_observe_sync", lambda r, s, sec: (True, ["o"]))
    assert ivc.run_full_check(["NIFTY"], 1)["passed"] is False


def test_observe_exception_is_caught(monkeypatch):
    monkeypatch.setattr(ivc, "check_resolution",
                        lambda syms: ({"NIFTY": {"security_id": 1}}, ["r"]))
    monkeypatch.setattr(ivc, "check_constants", lambda: (True, ["c"]))

    def _boom(*a):
        raise RuntimeError("socket down")
    monkeypatch.setattr(ivc, "_observe_sync", _boom)
    res = ivc.run_full_check(["NIFTY"], 1)
    assert res["passed"] is False
    assert any("failed to run" in ln for ln in res["lines"])


def test_run_and_report_persists_and_pushes(db, monkeypatch):
    pushed = []
    monkeypatch.setattr("app.engines.watchdog.push_ntfy",
                        lambda msg, kind: pushed.append((msg, kind)) or True)
    monkeypatch.setattr(ivc, "run_full_check",
                        lambda syms, seconds=45: {
                            "passed": True, "verdict": "PASS",
                            "lines": ["[3] NIFTY: volume=y OI=y"], "resolved": {}})
    res = ivc.run_and_report(["NIFTY"], seconds=1)
    assert res["passed"] is True
    stored = registry.setting("index_vol_check_result", "")
    assert stored.startswith("pass ")
    assert pushed and pushed[0][1] == "recovered"   # check-mark push on pass


def test_run_and_report_fail_pushes_down(db, monkeypatch):
    pushed = []
    monkeypatch.setattr("app.engines.watchdog.push_ntfy",
                        lambda msg, kind: pushed.append((msg, kind)) or True)
    monkeypatch.setattr(ivc, "run_full_check",
                        lambda syms, seconds=45: {
                            "passed": False, "verdict": "FAIL",
                            "lines": ["[3] NIFTY: volume=NO"], "resolved": {}})
    ivc.run_and_report(["NIFTY"], 1)
    assert registry.setting("index_vol_check_result", "").startswith("fail ")
    assert pushed and pushed[0][1] == "down"
