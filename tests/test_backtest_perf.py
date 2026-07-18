"""
Backtest performance regression: multi-timeframe history / chain() / iv_rank()
/ signal() must NOT hit DuckDB once per bar (step 2/1 fix — the original
implementation queried the store fresh on every call, which over a real
multi-year backtest ran into the tens of thousands of round-trips and
stalled/timed out real requests). Preload-once-then-bisect must keep the
query count roughly constant regardless of how many bars the strategy
examines.

Uses a real DataStore (DuckDB) with several months of bulk-inserted synthetic
data — close enough to a real backtest's shape to catch a regression that a
purely mocked test would miss, while staying fast enough for CI.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from app.core.contract import Action, ExpiryKind, LegSpec, OptionType, StrategyMeta
from app.data.store import DataStore
from app.engines import backtest as bt

IST_OPEN, IST_CLOSE = (9, 15), (15, 30)


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    # module-scoped + a modest day count: seeding thousands of rows across 4
    # tables is test-fixture overhead, not the code under test, and doesn't
    # need to be large to prove "query count doesn't scale with bar count" —
    # it just needs bar count to dwarf the expected (small, constant) query
    # count. Shared across both tests in this file to seed only once.
    s = DataStore(tmp_path_factory.mktemp("perf") / "perf.duckdb")
    _seed(s, days=20)
    return s


def _seed(store, days: int = 20) -> None:
    """Bulk-insert `days` trading days of 5-min NIFTY bars, ATM option_bars
    (IV), chain_snapshots and index_bias_history rows — enough for every read
    path under test to have real data to preload, not just empty fallbacks."""
    start = datetime(2026, 1, 5, 9, 15)   # a Monday
    underlying_rows, option_rows, chain_rows, bias_rows = [], [], [], []
    px = 22000.0
    d = 0
    day_count = 0
    while day_count < days:
        day = start + timedelta(days=d)
        d += 1
        if day.weekday() >= 5:
            continue
        day_count += 1
        ts = day
        for _ in range(75):   # 375 min / 5-min bars
            o = px
            px += ((_ % 7) - 3) * 1.5
            c = px
            underlying_rows.append(("NIFTY", ts, o, max(o, c) + 2,
                                    min(o, c) - 2, c, 1000.0, 0.0))
            if ts.minute % 15 == 0:
                iv = 14.0 + (ts.hour % 3)
                option_rows.append(("NIFTY", ts, "WEEKLY", 0, 0, "CALL",
                                    round(px / 50) * 50, None, 100.0, 100.0,
                                    100.0, 100.0, 100, 500, iv))
                chain_rows.append(("NIFTY", ts, None, "WEEKLY", 0,
                                   round(px / 50) * 50, 0, "CALL", px, 100.0,
                                   99.5, 100.5, iv, 500, 100, None, None,
                                   None, None))
            ts += timedelta(minutes=5)
        bias_rows.append((day.replace(hour=10), "NIFTY", 0.2, 0.3, 0.1,
                          20.0, 5.0, 25.0, 10, px))
    with store._lock:
        store.con.executemany(
            "INSERT OR REPLACE INTO underlying_bars VALUES (?,?,?,?,?,?,?,?)",
            underlying_rows)
        store.con.executemany(
            "INSERT OR REPLACE INTO option_bars VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", option_rows)
        store.con.executemany(
            "INSERT OR REPLACE INTO chain_snapshots VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", chain_rows)
        store.con.executemany(
            "INSERT OR REPLACE INTO index_bias_history VALUES "
            "(?,?,?,?,?,?,?,?,?,?)", bias_rows)


class _HeavyReader(bt.Strategy):
    """Calls every extended-surface read on EVERY bar — the worst case that
    stalled real backtests. Never trades (isolates the read-path cost)."""

    def __init__(self):
        self.params = {"warmup_bars": 30}
        self.calls = 0

    def meta(self):
        return StrategyMeta(name="heavy", underlying="NIFTY", timeframe="5",
                            params=self.params)

    def on_bar(self, ctx, bar):
        self.calls += 1
        ctx.history(25, interval=60)
        ctx.chain()
        ctx.iv_rank(30)
        ctx.signal("index_bias")
        ctx.signal("tier2")


def _query_counter(store):
    """Wrap store._q/_q1 to count real DuckDB round-trips."""
    counts = {"n": 0}
    orig_q, orig_q1 = store._q, store._q1

    def _q(sql, params=None):
        counts["n"] += 1
        return orig_q(sql, params)

    def _q1(sql, params=None):
        counts["n"] += 1
        return orig_q1(sql, params)

    store._q, store._q1 = _q, _q1
    return counts


def test_extended_reads_do_not_scale_with_bar_count(store):
    counts = _query_counter(store)
    strat = _HeavyReader()
    start = datetime(2026, 1, 5, 9, 15)
    end = start + timedelta(days=40)   # comfortably covers the 20 seeded trading days

    t0 = time.monotonic()
    res = bt.run_backtest(strat, store, start, end, 1_000_000)
    elapsed = time.monotonic() - t0

    assert "error" not in res
    assert strat.calls > 1000   # ~20 trading days worth of 5-min bars examined

    # THE regression guard: query count must stay near-constant (a handful of
    # one-time preloads), never scale with bars examined. Before the fix this
    # was ~4 queries * strat.calls (thousands), one per ctx.history/chain/
    # iv_rank/signal call on every bar; after the fix it's a small constant
    # number of preloads plus per-bar option-quote lookups already covered by
    # the existing preload path.
    assert counts["n"] < 50, (
        f"{counts['n']} store queries for {strat.calls} bars — "
        "extended reads are querying per-bar again, not preloading")

    # Loose wall-clock guard (CI-safe headroom): must finish in low seconds,
    # not the tens of seconds a per-bar query pattern would take even on this
    # small local dataset — a real multi-year VPS backtest would be
    # proportionally far worse, which is exactly what timed out in production.
    assert elapsed < 10.0, f"backtest took {elapsed:.1f}s"


def test_signal_and_history_values_match_direct_store_reads(store):
    """The preload+bisect path must return the SAME values the old per-call
    path did — a fast wrong answer is worse than a slow right one."""
    from app.engines.backtest import BacktestContext
    from app.engines import fills as F

    ctx = BacktestContext("NIFTY", store, 1_000_000, F.FeeConfig(), F.SlippageConfig())
    ctx._end = datetime(2026, 1, 30)      # end of the seeded window
    ctx._interval = 5
    ctx._replay.set_end(ctx._end)
    # bias_rows are stamped day.replace(hour=10) — start's minute (:15)
    # carries through, so the actual reading lands at 10:15, not 10:00.
    ts = datetime(2026, 1, 20, 10, 20)    # 5 min after that day's bias reading
    ctx.push_bar(bt.Bar(ts, 22000, 22010, 21990, 22000, 1000))

    cached_bias = ctx.signal("index_bias")
    direct_bias = store.index_bias_asof("NIFTY", ts)
    assert cached_bias is not None and direct_bias is not None
    assert cached_bias == direct_bias

    cached_htf = ctx.history(10, interval=60)
    direct_htf = store.history_bars("NIFTY", ts, 60, 10)
    assert cached_htf and direct_htf
    assert [b.ts for b in cached_htf] == [b.ts for b in direct_htf]
    assert [b.close for b in cached_htf] == [b.close for b in direct_htf]
