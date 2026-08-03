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

from datetime import date, datetime, timedelta

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


# --- sparse instruments must not cry wolf -----------------------------------
#
# 2026-08-03, from the phone: underlying_bars[GOLD] alerted at 14:55 and
# recovered at 15:09, then alerted at 15:55 and recovered at 15:58. Nothing
# that is actually broken recovers in three minutes — a tick simply arrived and
# the 5-min candle completed. CRUDEOIL, on the same feed and the same MCX
# session but far more liquid, was never flagged once.

MCX_SEGS = {"GOLD": "MCX", "CRUDEOIL": "MCX"}


def _bar_row(u, last_ts, rows_today, table="underlying_bars"):
    return {"table": table, "underlying": u, "present": True,
            "last_ts": last_ts.isoformat(sep=" "), "rows_today": rows_today}


def test_a_thin_contracts_quiet_stretch_is_not_an_outage():
    """GOLD at ~10 minutes between bars: 16 minutes of silence is normal for
    it, and used to fire the alarm."""
    now = datetime(2026, 8, 3, 15, 55)          # MCX open since 09:00 = 415 min
    gold = _bar_row("GOLD", now - timedelta(minutes=16), rows_today=41)   # ~10m avg
    assert rw.stale_underlyings([gold], now, MCX_SEGS,
                             {"underlying_bars"}) == []


def test_a_liquid_contract_keeps_the_tight_threshold():
    """CRUDEOIL writes every ~5 minutes, so 16 minutes of silence IS wrong and
    must still alert — the adaptive threshold can never relax below the flat
    one."""
    now = datetime(2026, 8, 3, 15, 55)
    crude = _bar_row("CRUDEOIL", now - timedelta(minutes=16), rows_today=83)
    assert rw.stale_underlyings([crude], now, MCX_SEGS,
                             {"underlying_bars"}) == ["underlying_bars[CRUDEOIL]"]


def test_a_genuinely_dead_thin_contract_still_trips():
    """The trade is DELAY, not blindness: a sparse name that really stops has
    an age that keeps growing past any threshold."""
    now = datetime(2026, 8, 3, 15, 55)
    gold = _bar_row("GOLD", now - timedelta(minutes=90), rows_today=41)
    assert rw.stale_underlyings([gold], now, MCX_SEGS,
                             {"underlying_bars"}) == ["underlying_bars[GOLD]"]


def test_the_threshold_is_never_looser_than_the_flat_one_for_a_busy_name():
    now = datetime(2026, 8, 3, 15, 55)
    busy = _bar_row("CRUDEOIL", now - timedelta(minutes=16), rows_today=400)
    assert rw.stale_underlyings([busy], now, MCX_SEGS,
                             {"underlying_bars"}) == ["underlying_bars[CRUDEOIL]"]


def test_too_few_rows_to_infer_a_cadence_falls_back_to_the_flat_clock():
    """One row says nothing about spacing; don't invent a cadence from it."""
    now = datetime(2026, 8, 3, 15, 55)
    thin = _bar_row("GOLD", now - timedelta(minutes=16), rows_today=1)
    assert rw.stale_underlyings([thin], now, MCX_SEGS,
                             {"underlying_bars"}) == ["underlying_bars[GOLD]"]


def test_never_written_is_still_stale_regardless_of_cadence():
    now = datetime(2026, 8, 3, 15, 55)
    row = {"table": "underlying_bars", "underlying": "GOLD", "present": True,
           "last_ts": None, "rows_today": 0}
    assert rw.stale_underlyings([row], now, MCX_SEGS,
                             {"underlying_bars"}) == ["underlying_bars[GOLD]"]


def test_the_multiple_is_what_sets_the_tolerance():
    """Hand-computed so the boundary is a deliberate decision, not a formula
    restated. Last write 15:00 = 360 min into the MCX session over 36 rows =
    a 10-minute cadence, so the tolerance is 3 x 10 = 30 minutes."""
    last = datetime(2026, 8, 3, 15, 0)
    row = _bar_row("GOLD", last, rows_today=36)
    inside = datetime(2026, 8, 3, 15, 29)        # 29 min quiet
    outside = datetime(2026, 8, 3, 15, 31)       # 31 min quiet
    assert rw.stale_underlyings([row], inside, MCX_SEGS, {"underlying_bars"}) == []
    assert rw.stale_underlyings([row], outside, MCX_SEGS,
                                {"underlying_bars"}) == ["underlying_bars[GOLD]"]


def test_a_name_that_died_early_is_not_given_a_looser_threshold():
    """The trap in measuring cadence to NOW: a name that stopped at 09:30 has
    few rows, which reads as 'writes rarely' and would RELAX the threshold
    exactly when it should tighten. Cadence is measured to the LAST WRITE."""
    now = datetime(2026, 8, 3, 15, 55)
    died = _bar_row("GOLD", datetime(2026, 8, 3, 9, 30), rows_today=10)
    assert rw.stale_underlyings([died], now, MCX_SEGS,
                                {"underlying_bars"}) == ["underlying_bars[GOLD]"]


def test_chain_tables_keep_the_flat_threshold():
    """Only underlying_bars adapts. chain_snapshots is written by the recorder
    on its own cycle whenever quotes move, so loosening it would blunt the
    detector that has caught seven chain outages."""
    now = datetime(2026, 8, 3, 15, 55)
    sparse_chain = _bar_row("GOLD", now - timedelta(minutes=20), rows_today=10,
                            table="chain_snapshots")
    assert rw.stale_underlyings([sparse_chain], now, MCX_SEGS,
                                {"chain_snapshots"}) == ["chain_snapshots[GOLD]"]
