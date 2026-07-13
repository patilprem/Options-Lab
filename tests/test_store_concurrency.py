"""The DuckDB store is shared across FastAPI threadpool requests. Concurrent
access on one connection returned None from a COUNT and 500'd a backtest when a
dashboard poll hit it mid-run. These reads must be serialised (thread-safe)."""

import threading
from datetime import datetime

from app.core.contract import Action, ExpiryKind, LegSpec, OptionType
from app.data.store import DataStore


def _seed(st):
    st.con.execute("INSERT INTO underlying_bars VALUES ('NIFTY', ?, 1,1,1,1,0,0)",
                   [datetime(2025, 1, 1, 9, 15)])
    st.con.execute(
        "INSERT INTO option_bars VALUES ('NIFTY', ?, 'WEEKLY',0,0,'CALL',20000,NULL,"
        "100,100,100,100,0,0,12)", [datetime(2025, 1, 1, 9, 15)])


def test_datastore_reads_are_thread_safe(tmp_path):
    st = DataStore(tmp_path / "m.duckdb")
    _seed(st)
    leg = LegSpec(OptionType.CALL, Action.SELL, 0, ExpiryKind.WEEKLY, 0, 1)
    a, b = datetime(2025, 1, 1), datetime(2025, 1, 2)

    errors = []

    def worker():
        try:
            for _ in range(60):
                assert st.has_data("NIFTY", a, b) is True
                st.underlying_bars("NIFTY", a, b, 5)
                st.option_close("NIFTY", datetime(2025, 1, 1, 10, 0), leg)
                st.coverage()
        except Exception as e:      # the pre-fix bug: 'NoneType' not subscriptable
            errors.append(repr(e))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors[:3]
    # still correct after the hammering
    assert st.has_data("NIFTY", a, b) is True
    rows, opt = st.coverage()
    assert opt["NIFTY"] == 1
