"""Per-underlying recording alerts — the granularity that can see a masked freeze.

2026-07-31, the incident this exists for: NIFTY, BANKNIFTY, CRUDEOIL and GOLD
stopped recording chains at 09:30 and stayed dead for 90 minutes. The
table-level watchdog never flagged chain_snapshots ONCE, because INFY and TCS
were open scanner positions and their Tier-2 deep-dives kept writing to the
same table. The only alert that fired said "option_bars" — and only because
option_bars happens to have a single writer.

So the push named a table nobody can act on, while the four names that were
actually dead went unnamed for an hour and a half. No threshold tuning fixes
that: a table aggregated over two independent writers is simply the wrong unit
to judge. These tests pin the right one.
"""

from datetime import date, datetime

import pytest

from app.data.store import DataStore
from app.engines import recording_watchdog as rw

# Friday 2026-07-31 11:00 — NSE and MCX both open, well past the grace period.
NOW = datetime(2026, 7, 31, 11, 0)
CORE = ["BANKNIFTY", "CRUDEOIL", "GOLD", "NIFTY"]
SEGMENTS = {"NIFTY": "NSE", "BANKNIFTY": "NSE", "CRUDEOIL": "MCX", "GOLD": "MCX"}


def _row(table, underlying, last_ts):
    return {"table": table, "underlying": underlying, "present": True,
            "last_ts": None if last_ts is None else str(last_ts),
            "rows_today": 0 if last_ts is None else 10, "error": None}


# --- the masking scenario, reproduced ---------------------------------------

def test_frozen_core_names_are_caught_while_the_table_looks_fresh():
    """THE regression. chain_snapshots passes the table-level check (stocks are
    writing), so it is in fresh_tables — and the four core names must still be
    reported individually."""
    rows = [_row("chain_snapshots", u, datetime(2026, 7, 31, 9, 30)) for u in CORE]
    stale = rw.stale_underlyings(rows, NOW, SEGMENTS, {"chain_snapshots"})
    assert stale == ["chain_snapshots[BANKNIFTY]", "chain_snapshots[CRUDEOIL]",
                     "chain_snapshots[GOLD]", "chain_snapshots[NIFTY]"]


def test_the_alert_names_the_underlyings_not_just_the_table():
    """End to end through step(): the push text must say which names are dead."""
    sent = []
    wd = rw.RecordingWatchdog(notify=lambda msg, kind: sent.append((msg, kind)) or True)
    health = [{"table": "chain_snapshots", "present": True, "periodic": True,
               "segments": ["NSE", "MCX"],
               "last_ts": str(datetime(2026, 7, 31, 10, 59))}]      # fresh
    rows = [_row("chain_snapshots", u, datetime(2026, 7, 31, 9, 30)) for u in CORE]

    assert wd.step(health, NOW, rows, SEGMENTS) == "stale"
    msg = sent[0][0]
    for u in CORE:
        assert f"chain_snapshots[{u}]" in msg, msg


def test_a_healthy_core_name_is_not_flagged_when_its_peers_are():
    """Only the dead ones get named — a push that over-reports is one you stop
    reading."""
    rows = [_row("chain_snapshots", u, datetime(2026, 7, 31, 9, 30))
            for u in ("NIFTY", "BANKNIFTY")]
    rows += [_row("chain_snapshots", u, datetime(2026, 7, 31, 10, 58))
             for u in ("CRUDEOIL", "GOLD")]
    stale = rw.stale_underlyings(rows, NOW, SEGMENTS, {"chain_snapshots"})
    assert stale == ["chain_snapshots[BANKNIFTY]", "chain_snapshots[NIFTY]"]


def test_never_written_underlying_is_stale_not_skipped():
    """last_ts=None means the recorder has never written this name at all —
    the loudest possible state, and it must not be silently skipped the way a
    missing GROUP BY row would be."""
    rows = [_row("chain_snapshots", "NIFTY", None)]
    assert rw.stale_underlyings(rows, NOW, SEGMENTS, {"chain_snapshots"}) \
        == ["chain_snapshots[NIFTY]"]


# --- no double-reporting, no wrong-hours noise -------------------------------

def test_table_already_stale_is_not_also_reported_per_underlying():
    """If stale_tables() already named the table, listing every underlying
    inside it adds length, not information."""
    rows = [_row("option_bars", u, datetime(2026, 7, 31, 9, 30)) for u in CORE]
    assert rw.stale_underlyings(rows, NOW, SEGMENTS, fresh_tables=set()) == []


def test_step_reports_the_table_once_when_it_is_wholly_stale():
    sent = []
    wd = rw.RecordingWatchdog(notify=lambda msg, kind: sent.append((msg, kind)) or True)
    health = [{"table": "option_bars", "present": True, "periodic": True,
               "segments": ["NSE", "MCX"],
               "last_ts": str(datetime(2026, 7, 31, 9, 30))}]        # stale
    rows = [_row("option_bars", u, datetime(2026, 7, 31, 9, 30)) for u in CORE]
    assert wd.step(health, NOW, rows, SEGMENTS) == "stale"
    assert "option_bars" in sent[0][0]
    assert "option_bars[NIFTY]" not in sent[0][0]


def test_mcx_names_are_not_flagged_after_the_nse_close():
    """17:00 IST: NSE shut, MCX trades until 23:30. The index names are
    correctly idle; the commodities are genuinely stale. Judging commodities
    against NSE hours would have flagged them every evening (the mistake
    /data/health made on its first night)."""
    evening = datetime(2026, 7, 31, 17, 0)
    rows = [_row("chain_snapshots", u, datetime(2026, 7, 31, 15, 25)) for u in CORE]
    stale = rw.stale_underlyings(rows, evening, SEGMENTS, {"chain_snapshots"})
    assert stale == ["chain_snapshots[CRUDEOIL]", "chain_snapshots[GOLD]"]


def test_nothing_is_flagged_on_a_weekend():
    saturday = datetime(2026, 8, 1, 11, 0)
    rows = [_row("chain_snapshots", u, datetime(2026, 7, 31, 15, 25)) for u in CORE]
    assert rw.stale_underlyings(rows, saturday, SEGMENTS, {"chain_snapshots"}) == []


# --- backward compatibility --------------------------------------------------

def test_step_without_underlying_rows_behaves_exactly_as_before():
    """Callers (and the existing test suite) that pass only table health must
    be completely unaffected."""
    sent = []
    wd = rw.RecordingWatchdog(notify=lambda msg, kind: sent.append((msg, kind)) or True)
    health = [{"table": "option_bars", "present": True, "periodic": True,
               "segments": ["NSE", "MCX"],
               "last_ts": str(datetime(2026, 7, 31, 9, 30))}]
    assert wd.step(health, NOW) == "stale"
    assert wd.stale == ("option_bars",)


def test_recovery_clears_after_a_per_underlying_alert():
    sent = []
    wd = rw.RecordingWatchdog(notify=lambda msg, kind: sent.append((msg, kind)) or True)
    health = [{"table": "chain_snapshots", "present": True, "periodic": True,
               "segments": ["NSE", "MCX"],
               "last_ts": str(datetime(2026, 7, 31, 10, 59))}]
    frozen = [_row("chain_snapshots", u, datetime(2026, 7, 31, 9, 30)) for u in CORE]
    assert wd.step(health, NOW, frozen, SEGMENTS) == "stale"

    caught_up = [_row("chain_snapshots", u, datetime(2026, 7, 31, 10, 59)) for u in CORE]
    assert wd.step(health, NOW, caught_up, SEGMENTS) == "recovered"
    assert wd.stale == ()


# --- the store query behind it -----------------------------------------------

def _store(tmp_path):
    st = DataStore(tmp_path / "m.duckdb")
    rows = []
    # core names frozen at 09:30, a stock still writing at 10:59 — exactly the
    # shape that fooled the table-level check
    for u in CORE:
        rows.append((u, datetime(2026, 7, 31, 9, 30), date(2026, 8, 27), "WEEKLY",
                     0, 100.0, 0, "CALL", 100.0, 5.0, 4.9, 5.1, 12.0, 100.0,
                     10.0, 0.5, -0.1, 0.2, 0.01))
    rows.append(("INFY", datetime(2026, 7, 31, 10, 59), date(2026, 8, 27), "MONTHLY",
                 0, 100.0, 0, "CALL", 100.0, 5.0, 4.9, 5.1, 12.0, 100.0,
                 10.0, 0.5, -0.1, 0.2, 0.01))
    st.upsert_chain_rows(rows)
    return st


def test_store_reports_each_requested_underlying(tmp_path):
    st = _store(tmp_path)
    rows = st.recording_underlying_health(CORE, tables=("chain_snapshots",))
    got = {r["underlying"]: r["last_ts"] for r in rows}
    assert sorted(got) == CORE
    for u in CORE:
        assert got[u].startswith("2026-07-31 09:30")


def test_store_reports_a_requested_name_with_no_rows_at_all(tmp_path):
    st = _store(tmp_path)
    rows = st.recording_underlying_health(["NIFTY", "SILVER"],
                                          tables=("chain_snapshots",))
    got = {r["underlying"]: r["last_ts"] for r in rows}
    assert got["SILVER"] is None          # never recorded — must still appear
    assert got["NIFTY"] is not None


def test_store_ignores_underlyings_it_was_not_asked_about(tmp_path):
    """INFY is writing to the same table; it must not appear in a core-name
    audit, and must not make the core names look fine either."""
    st = _store(tmp_path)
    rows = st.recording_underlying_health(CORE, tables=("chain_snapshots",))
    assert "INFY" not in {r["underlying"] for r in rows}


def test_store_returns_empty_for_no_names(tmp_path):
    st = _store(tmp_path)
    assert st.recording_underlying_health([]) == []


def test_end_to_end_store_to_alert(tmp_path):
    """The real query feeding the real watchdog: the table is fresh (INFY), the
    core names are 90 minutes cold, and the alert names all four."""
    st = _store(tmp_path)
    rows = st.recording_underlying_health(CORE, tables=("chain_snapshots",))
    stale = rw.stale_underlyings(rows, NOW, SEGMENTS, {"chain_snapshots"})
    assert stale == [f"chain_snapshots[{u}]" for u in CORE]


@pytest.mark.parametrize("underlying,expected", [
    ("NIFTY", "NSE"), ("BANKNIFTY", "NSE"), ("CRUDEOIL", "MCX"), ("GOLD", "MCX"),
])
def test_segment_for_matches_the_recorders_own_session_gate(underlying, expected):
    assert rw.segment_for(underlying) == expected
