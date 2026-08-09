"""The migration that relabels chain rows recorded as WEEKLY but holding
MONTHLY contracts (scripts/relabel_chain_expiry_kind).

CHAIN_TARGETS asked every underlying for ("WEEKLY", 0/1). BANKNIFTY's weeklies
are discontinued and MCX is monthly-only, so resolve_expiry handed back their
MONTHLY contract and we persisted it as WEEKLY. The live path now derives the
kind from the expiry list; this covers the history already on disk.

The rule has to be data-driven both ways: BANKNIFTY genuinely HAD weeklies
until NSE dropped them, so relabelling every BANKNIFTY row would corrupt the
older ones. No network, no service — an in-memory DuckDB.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import duckdb
import pytest

from app.data.store import SCHEMA
from scripts.relabel_chain_expiry_kind import monthly_only_expiries, run

D = date


# ---------------------------------------------------------------------------
# the rule
# ---------------------------------------------------------------------------

def test_monthly_gaps_are_monthly_only():
    """BANKNIFTY post-discontinuation: the recorded expiries only ever advance
    a month at a time."""
    rows = [(D(2026, 7, 28), D(2026, 7, 1), D(2026, 7, 28)),
            (D(2026, 8, 25), D(2026, 7, 29), D(2026, 8, 25))]
    monthly, undecided = monthly_only_expiries(rows)
    assert monthly == [D(2026, 7, 28), D(2026, 8, 25)]
    assert undecided == []


def test_a_real_weekly_cycle_is_left_alone():
    """NIFTY, and BANKNIFTY before Nov 2024. Relabelling these would be the
    corruption this script exists to avoid."""
    rows = [(D(2026, 8, 6), D(2026, 7, 31), D(2026, 8, 6)),
            (D(2026, 8, 13), D(2026, 8, 7), D(2026, 8, 13)),
            (D(2026, 8, 20), D(2026, 8, 14), D(2026, 8, 20))]
    monthly, undecided = monthly_only_expiries(rows)
    assert monthly == []
    assert undecided == []


def test_a_holiday_skipped_week_stays_weekly():
    rows = [(D(2026, 8, 5), D(2026, 7, 30), D(2026, 8, 5)),
            (D(2026, 8, 19), D(2026, 8, 6), D(2026, 8, 19))]
    assert monthly_only_expiries(rows)[0] == []


def test_a_lone_expiry_held_for_a_month_is_monthly():
    """SPAN carries the case NEIGHBOUR cannot: a name whose whole history holds
    one expiry. A weekly is the front contract for about a week."""
    rows = [(D(2026, 8, 25), D(2026, 7, 27), D(2026, 8, 25))]
    monthly, undecided = monthly_only_expiries(rows)
    assert monthly == [D(2026, 8, 25)]
    assert undecided == []


def test_a_lone_expiry_recorded_briefly_is_undecided():
    """Three days of history proves nothing either way. Under-claiming is
    recoverable; relabelling a real weekly is not."""
    rows = [(D(2026, 8, 6), D(2026, 8, 3), D(2026, 8, 5))]
    monthly, undecided = monthly_only_expiries(rows)
    assert monthly == []
    assert undecided == [D(2026, 8, 6)]


def test_span_catches_a_monthly_sitting_next_to_a_weekly():
    """At a regime change the first monthly can land within a weekly gap of the
    last weekly. NEIGHBOUR clears it; SPAN still catches it."""
    rows = [(D(2024, 11, 13), D(2024, 11, 7), D(2024, 11, 13)),   # last weekly
            (D(2024, 11, 27), D(2024, 11, 14), D(2024, 12, 20))]  # first monthly
    monthly, _ = monthly_only_expiries(rows)
    assert monthly == [D(2024, 11, 27)], "the weekly must survive, the monthly must not"


def test_null_expiries_are_ignored():
    assert monthly_only_expiries([(None, D(2026, 8, 1), D(2026, 9, 1))]) == ([], [])


# ---------------------------------------------------------------------------
# end to end against a real DuckDB
# ---------------------------------------------------------------------------

OPT_COLS = ("underlying, ts, expiry_kind, expiry_offset, strike_offset, "
            "option_type, strike, expiry, open, high, low, close, volume, "
            "oi, iv")
SNAP_COLS = ("underlying, ts, expiry, expiry_kind, expiry_offset, strike, "
             "strike_offset, option_type, spot, ltp, bid, ask, iv, oi, "
             "volume, delta, theta, vega, gamma")


def _opt(con, u, day, expiry, kind="WEEKLY", strike_offset=0):
    con.execute(
        f"INSERT INTO option_bars ({OPT_COLS}) VALUES "
        f"(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [u, datetime.combine(day, datetime.min.time()).replace(hour=10),
         kind, 0, strike_offset, "CALL", 100.0, expiry,
         1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 12.0])


def _snap(con, u, day, expiry, kind="WEEKLY"):
    con.execute(
        f"INSERT INTO chain_snapshots ({SNAP_COLS}) VALUES "
        f"(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [u, datetime.combine(day, datetime.min.time()).replace(hour=10),
         expiry, kind, 0, 100.0, 0, "CALL", 100.0,
         1.0, 1.0, 1.0, 12.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])


def _db():
    con = duckdb.connect(":memory:")
    con.execute(SCHEMA)
    # BANKNIFTY: two expiries a month apart, each held ~a month -> monthly-only
    for i in range(20):
        _opt(con, "BANKNIFTY", D(2026, 7, 1) + timedelta(days=i), D(2026, 7, 28))
        _snap(con, "BANKNIFTY", D(2026, 7, 1) + timedelta(days=i), D(2026, 7, 28))
    for i in range(20):
        _opt(con, "BANKNIFTY", D(2026, 7, 29) + timedelta(days=i), D(2026, 8, 25))
    # NIFTY: a genuine weekly cadence -> must survive untouched
    for i in range(5):
        _opt(con, "NIFTY", D(2026, 8, 3) + timedelta(days=i), D(2026, 8, 6))
    for i in range(5):
        _opt(con, "NIFTY", D(2026, 8, 10) + timedelta(days=i), D(2026, 8, 13))
    return con


def _args(tmp_path, yes=False, underlying=""):
    return SimpleNamespace(underlying=underlying, yes=yes,
                           backup_dir=str(tmp_path / "backups"))


def _kinds(con, u):
    return dict(con.execute(
        "SELECT expiry_kind, count(*) FROM option_bars WHERE underlying = ? "
        "GROUP BY 1", [u]).fetchall())


def test_dry_run_changes_nothing(tmp_path, capsys):
    con = _db()
    assert run(con, _args(tmp_path)) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert _kinds(con, "BANKNIFTY") == {"WEEKLY": 40}


def test_relabels_the_monthly_only_underlying(tmp_path):
    con = _db()
    assert run(con, _args(tmp_path, yes=True)) == 0
    assert _kinds(con, "BANKNIFTY") == {"MONTHLY": 40}


def test_leaves_a_genuine_weekly_underlying_alone(tmp_path):
    con = _db()
    run(con, _args(tmp_path, yes=True))
    assert _kinds(con, "NIFTY") == {"WEEKLY": 10}


def test_relabels_chain_snapshots_too(tmp_path):
    con = _db()
    run(con, _args(tmp_path, yes=True))
    kinds = dict(con.execute(
        "SELECT expiry_kind, count(*) FROM chain_snapshots GROUP BY 1").fetchall())
    assert kinds == {"MONTHLY": 20}


def test_the_backup_reads_back_and_restores(tmp_path):
    """The whole point of the Parquet step: this is reversible."""
    con = _db()
    run(con, _args(tmp_path, yes=True))
    backups = list((tmp_path / "backups").rglob("option_bars.BANKNIFTY.parquet"))
    assert backups, "no backup written"
    n = con.execute("SELECT count(*) FROM read_parquet(?)",
                    [str(backups[0])]).fetchone()[0]
    assert n == 40
    kinds = dict(con.execute(
        "SELECT expiry_kind, count(*) FROM read_parquet(?) GROUP BY 1",
        [str(backups[0])]).fetchall())
    assert kinds == {"WEEKLY": 40}, "the backup must hold the ORIGINAL labels"


def test_scoping_to_one_underlying(tmp_path):
    con = _db()
    run(con, _args(tmp_path, yes=True, underlying="NIFTY"))
    assert _kinds(con, "BANKNIFTY") == {"WEEKLY": 40}


def test_a_primary_key_collision_aborts_before_changing_anything(tmp_path, capsys):
    """A MONTHLY twin at the same PK would make the UPDATE raise halfway
    through. Refuse up front instead."""
    con = _db()
    con.execute("UPDATE option_bars SET expiry_kind = 'MONTHLY' "
                "WHERE underlying = 'BANKNIFTY' AND expiry = DATE '2026-07-28' "
                "AND ts = (SELECT min(ts) FROM option_bars "
                "          WHERE underlying = 'BANKNIFTY')")
    _opt(con, "BANKNIFTY", D(2026, 7, 1), D(2026, 7, 28))   # WEEKLY twin
    assert run(con, _args(tmp_path, yes=True)) == 1
    assert "primary key" in capsys.readouterr().out
    assert _kinds(con, "BANKNIFTY") == {"WEEKLY": 40, "MONTHLY": 1}, \
        "the run must abort with the table exactly as it found it"
    assert _kinds(con, "NIFTY") == {"WEEKLY": 10}


def test_an_empty_database_is_a_clean_no_op(tmp_path, capsys):
    con = duckdb.connect(":memory:")
    con.execute(SCHEMA)
    assert run(con, _args(tmp_path)) == 0
    assert "nothing to relabel" in capsys.readouterr().out
