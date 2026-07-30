"""registry.record_event() must never block the event loop it's called from.

2026-07-30: /scanner and /portfolio/today hung for a full 15s client timeout
with zero bytes back, then 5/5 immediate retries succeeded in ~9ms each —
intermittent, self-clearing. record_event() was confirmed to run synchronously
on the event loop from its (many) async callers, never wrapped in
run_in_executor anywhere. Under WAL, a colliding writer can legitimately block
for up to busy_timeout=8000 (8s); on a single-worker uvicorn process, that
freezes EVERYTHING sharing the loop, including handing new HTTP requests off
to FastAPI's own separate thread pool.

Fix: from a running event loop, dispatch the actual write to a worker thread
and return immediately (fire-and-forget — callers never awaited this, and
never should have to). From sync context (FastAPI request handlers, scripts,
most tests), nothing changes: the write still happens inline.
"""

import asyncio
import time

from app.core import registry


def test_sync_call_writes_immediately_and_synchronously(tmp_path, monkeypatch):
    """The overwhelming majority of existing call sites (tests included) call
    this from plain sync code and expect the row to exist the instant the
    call returns. That behavior must be completely unchanged."""
    db = tmp_path / "t.db"
    monkeypatch.setattr(registry, "DB_PATH", db)
    registry.init_db()

    registry.record_event("info", "test", "sync write")

    with registry._conn() as c:
        rows = c.execute("SELECT message FROM events").fetchall()
    assert [r[0] for r in rows] == ["sync write"]


def test_async_call_does_not_block_the_loop(tmp_path, monkeypatch):
    """THE regression. Calling record_event from inside a coroutine must
    return near-instantly, not block on the SQLite write."""
    db = tmp_path / "t.db"
    monkeypatch.setattr(registry, "DB_PATH", db)
    registry.init_db()

    async def run():
        t0 = time.monotonic()
        registry.record_event("info", "test", "async write")
        return time.monotonic() - t0

    elapsed = asyncio.run(run())
    assert elapsed < 0.05, (
        f"record_event() took {elapsed:.3f}s from async context — it must "
        f"return immediately, dispatching the actual write to a thread")


def test_async_call_eventually_persists(tmp_path, monkeypatch):
    """Fire-and-forget must still actually forget-and-fire: the row must
    show up shortly after, even though the caller didn't wait for it."""
    db = tmp_path / "t.db"
    monkeypatch.setattr(registry, "DB_PATH", db)
    registry.init_db()

    async def run():
        registry.record_event("info", "test", "eventually there")
        # give the background thread a moment to actually run — this is not
        # awaiting the write itself (we deliberately have no handle to it),
        # just yielding so a fast background thread gets a chance to execute.
        for _ in range(50):
            await asyncio.sleep(0.02)
            with registry._conn() as c:
                if c.execute("SELECT count(*) FROM events").fetchone()[0]:
                    return True
        return False

    assert asyncio.run(run()), "background write never landed within 1s"


def test_concurrent_async_writes_do_not_lose_events(tmp_path, monkeypatch):
    """Many events firing in a tight burst (exactly today's shape — several
    no-op warnings within seconds of each other) must all eventually land,
    not silently drop under concurrent dispatch."""
    db = tmp_path / "t.db"
    monkeypatch.setattr(registry, "DB_PATH", db)
    registry.init_db()

    async def run():
        for i in range(20):
            registry.record_event("info", "test", f"burst {i}")
        for _ in range(100):
            await asyncio.sleep(0.02)
            with registry._conn() as c:
                n = c.execute("SELECT count(*) FROM events").fetchone()[0]
            if n == 20:
                return n
        return n

    assert asyncio.run(run()) == 20


def test_sync_call_from_a_thread_with_no_loop_still_works(tmp_path, monkeypatch):
    """A background thread that isn't running an event loop (e.g. work
    dispatched via run_in_executor itself) must fall back to a direct write,
    not raise trying to find a loop that doesn't exist there."""
    import threading

    db = tmp_path / "t.db"
    monkeypatch.setattr(registry, "DB_PATH", db)
    registry.init_db()

    errors = []

    def worker():
        try:
            registry.record_event("info", "test", "from a bare thread")
        except Exception as e:
            errors.append(e)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=2)
    assert not errors, errors
    with registry._conn() as c:
        rows = c.execute("SELECT message FROM events").fetchall()
    assert [r[0] for r in rows] == ["from a bare thread"]
