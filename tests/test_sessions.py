"""Session-window gate: the 2026-07-27 incident, pinned in both directions.

Bug A — MCX ticks after 15:30 were discarded (NSE's window applied to every
underlying), so underlying_bars held no MCX history at all.
Bug B — flat zero-volume phantom candles stamped AFTER the NSE close reached
underlying_bars through the backfill, which had no gate whatsoever.

A fix for one must not reintroduce the other, so both are asserted here.
"""

from datetime import datetime

from app.data import dhan_client as dc
from app.data.sessions import in_session

# 2026-07-27 is a Monday; 2026-08-01 a Saturday.
MON = "2026-07-27"


def _ts(hhmm: str, day: str = MON) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00")


# --- the window itself ------------------------------------------------------

def test_nse_regular_session_bounds():
    assert not in_session(_ts("09:14"), segment="NSE")   # pre-open auction
    assert in_session(_ts("09:15"), segment="NSE")       # inclusive open
    assert in_session(_ts("15:30"), segment="NSE")       # inclusive close
    assert not in_session(_ts("15:35"), segment="NSE")
    # Bug B: the exact phantom-bar timestamps observed in production.
    assert not in_session(_ts("16:05"), segment="NSE")
    assert not in_session(_ts("16:10"), segment="NSE")


def test_mcx_runs_far_later_than_nse():
    # Bug A: every one of these was thrown away by the NSE-only window.
    for hhmm in ("09:00", "15:31", "17:00", "20:00", "23:30"):
        assert in_session(_ts(hhmm), segment="MCX"), hhmm
    assert not in_session(_ts("08:59"), segment="MCX")
    assert not in_session(_ts("23:35"), segment="MCX")


def test_weekend_is_never_in_session():
    for seg in ("NSE", "MCX"):
        assert not in_session(_ts("11:00", "2026-08-01"), segment=seg)  # Sat
        assert not in_session(_ts("11:00", "2026-08-02"), segment=seg)  # Sun


def test_unknown_underlying_gets_the_stricter_window():
    """Letting junk in is worse than dropping a bar for an unconfigured name."""
    assert in_session(_ts("11:00"), "NOT_A_REAL_SYMBOL")
    assert not in_session(_ts("17:00"), "NOT_A_REAL_SYMBOL")


# --- dated overrides: holidays / modified sessions --------------------------
# 2026-09-14 is a Monday, so the regular weekly window would otherwise apply.

HOLIDAY = "2026-09-14"


def test_holiday_closes_nse_all_day():
    for hhmm in ("00:00", "09:15", "12:00", "15:30", "23:59"):
        assert not in_session(_ts(hhmm, HOLIDAY), segment="NSE"), hhmm


def test_holiday_delays_mcx_open_but_keeps_its_close():
    # MCX's regular 09:00 open doesn't apply today -- trading starts at 17:00.
    for hhmm in ("09:00", "12:00", "16:59"):
        assert not in_session(_ts(hhmm, HOLIDAY), segment="MCX"), hhmm
    for hhmm in ("17:00", "20:00", "23:30"):
        assert in_session(_ts(hhmm, HOLIDAY), segment="MCX"), hhmm
    assert not in_session(_ts("23:31", HOLIDAY), segment="MCX")


def test_a_non_overridden_date_is_unaffected():
    """The override table must not leak into any other date."""
    assert in_session(_ts("09:00", MON), segment="MCX")
    assert in_session(_ts("09:15", MON), segment="NSE")


def test_segment_resolved_from_underlyings_table():
    assert in_session(_ts("11:00"), "NIFTY")
    assert not in_session(_ts("17:00"), "NIFTY")


def test_mcx_underlying_resolves_to_the_late_window():
    """MCX names are added to UNDERLYINGS at runtime by the dynamic resolver,
    so the lookup must key off the segment string, not a static allow-list."""
    dc.UNDERLYINGS["_TEST_MCX"] = {"security_id": 1, "segment": "MCX_COMM"}
    try:
        assert in_session(_ts("17:00"), "_TEST_MCX")
        assert in_session(_ts("23:30"), "_TEST_MCX")
        assert not in_session(_ts("08:00"), "_TEST_MCX")
    finally:
        dc.UNDERLYINGS.pop("_TEST_MCX")


# --- the write path (bug B's actual entry point) ----------------------------

class _MemStore:
    def __init__(self):
        import duckdb
        self.con = duckdb.connect(":memory:")
        self.con.execute("""CREATE TABLE underlying_bars (
            underlying VARCHAR, ts TIMESTAMP, open DOUBLE, high DOUBLE,
            low DOUBLE, close DOUBLE, volume DOUBLE, oi DOUBLE,
            PRIMARY KEY (underlying, ts))""")


def _row(underlying, ts, px=100.0, vol=0.0):
    return (underlying, ts, px, px, px, px, vol, 0)


def test_backfill_drops_post_close_phantom_bars():
    """The production shape: a real session plus one flat zero-volume bar
    stamped after the close. Only the real bars may land."""
    store = _MemStore()
    rows = [_row("NIFTY", _ts("09:15"), 23990.0, 1e6),
            _row("NIFTY", _ts("15:25"), 23995.0, 2e6),
            _row("NIFTY", _ts("16:10"), 23995.95, 0.0)]   # the phantom
    assert dc.upsert_underlying_rows(store, rows) == 2
    kept = store.con.execute(
        "SELECT ts FROM underlying_bars ORDER BY ts").fetchall()
    assert [r[0] for r in kept] == [_ts("09:15"), _ts("15:25")]


def test_backfill_keeps_mcx_evening_bars():
    """The guard must not resurrect bug A on the write path."""
    dc.UNDERLYINGS["_TEST_MCX"] = {"security_id": 1, "segment": "MCX_COMM"}
    try:
        store = _MemStore()
        rows = [_row("_TEST_MCX", _ts("17:00")),
                _row("_TEST_MCX", _ts("22:45")),
                _row("_TEST_MCX", _ts("23:45"))]          # past MCX close
        assert dc.upsert_underlying_rows(store, rows) == 2
    finally:
        dc.UNDERLYINGS.pop("_TEST_MCX")


def test_backfill_with_no_rows_is_a_noop():
    assert dc.upsert_underlying_rows(_MemStore(), []) == 0


def test_live_bar_write_is_gated_too():
    """store.upsert_live_bar is the other writer; same rule applies."""
    from app.core.contract import Bar
    from app.data.store import DataStore

    store = DataStore.__new__(DataStore)              # skip DuckDB file setup
    import threading, duckdb
    store._lock = threading.Lock()
    store.con = duckdb.connect(":memory:")
    store.con.execute("""CREATE TABLE underlying_bars (
        underlying VARCHAR, ts TIMESTAMP, open DOUBLE, high DOUBLE,
        low DOUBLE, close DOUBLE, volume DOUBLE, oi DOUBLE,
        PRIMARY KEY (underlying, ts))""")

    def bar(ts):
        return Bar(ts=ts, open=1.0, high=1.0, low=1.0, close=1.0, volume=0)

    store.upsert_live_bar("NIFTY", bar(_ts("11:00")))
    store.upsert_live_bar("NIFTY", bar(_ts("16:10")))    # phantom
    n = store.con.execute("SELECT count(*) FROM underlying_bars").fetchone()[0]
    assert n == 1


# --- the rest of the 2026 NSE holiday-master ---------------------------------
# Source: NSE holiday-master (FO list for NSE/BSE, COM list for MCX).

from datetime import date  # noqa: E402

from app.data.sessions import SESSION_OVERRIDES, session_window  # noqa: E402

# NSE/BSE closed all day AND both MCX sessions closed.
FULL_CLOSURE = ["2026-01-26", "2026-04-03", "2026-10-02", "2026-12-25"]
# NSE/BSE closed all day; MCX loses only its morning session (opens 17:00).
MCX_EVENING_ONLY = ["2026-01-15", "2026-03-03", "2026-03-26", "2026-03-31",
                    "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
                    "2026-09-14", "2026-10-20", "2026-11-10", "2026-11-24"]


def test_full_closures_close_both_segments_all_day():
    for day in FULL_CLOSURE:
        for seg in ("NSE", "MCX"):
            for hhmm in ("09:00", "11:00", "17:00", "20:00", "23:30"):
                assert not in_session(_ts(hhmm, day), segment=seg), (day, seg, hhmm)


def test_mcx_evening_only_holidays():
    for day in MCX_EVENING_ONLY:
        for hhmm in ("09:15", "12:00", "16:59"):
            assert not in_session(_ts(hhmm, day), segment="NSE"), (day, hhmm)
            assert not in_session(_ts(hhmm, day), segment="MCX"), (day, hhmm)
        assert not in_session(_ts("19:00", day), segment="NSE"), day
        assert in_session(_ts("17:00", day), segment="MCX"), day
        assert in_session(_ts("23:30", day), segment="MCX"), day
        assert not in_session(_ts("23:31", day), segment="MCX"), day


def test_new_year_mcx_morning_only_nse_normal():
    """2026-01-01 (Thu): NSE trades normally, MCX's evening session is shut."""
    day = "2026-01-01"
    assert in_session(_ts("09:15", day), segment="NSE")
    assert in_session(_ts("15:30", day), segment="NSE")
    assert in_session(_ts("09:00", day), segment="MCX")
    assert in_session(_ts("16:55", day), segment="MCX")
    assert not in_session(_ts("17:00", day), segment="MCX")
    assert not in_session(_ts("20:00", day), segment="MCX")


def test_every_override_is_a_weekday_with_a_sane_window():
    """A weekend entry is dead weight (in_session rejects weekends first) and
    a window with open >= close would silently close the segment."""
    for (seg, iso), window in SESSION_OVERRIDES.items():
        assert seg in ("NSE", "MCX"), (seg, iso)
        assert date.fromisoformat(iso).weekday() < 5, (seg, iso)
        if window is not None:
            assert window[0] < window[1], (seg, iso)


def test_holiday_table_agrees_with_session_window():
    for day in FULL_CLOSURE:
        assert session_window("NSE", date.fromisoformat(day)) is None
        assert session_window("MCX", date.fromisoformat(day)) is None
