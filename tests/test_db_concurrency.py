"""optionslab.db must not block readers behind writers.

2026-07-28 ~13:00: /token/status, /data/health and /scanner all timed out
while chain_snapshots kept writing normally — data flowing, HTTP dead. The
same session's fixes had just added roughly a dozen new registry.record_event()
call sites (the recording watchdog, four loop guards, crash-loop containment,
entry-skip logging), and optionslab.db had never had WAL mode or a
busy_timeout: in the default rollback-journal mode a writer's transaction
blocks EVERY reader for its duration, so more writers meant more chances for a
reader to stall behind one. It also explains the intermittent
"Dhan credentials not found" seen in the same window: resolve_credentials()
swallows any exception from the token lookup, so a locked-database error there
silently becomes "credentials not found" even with a valid token.

These tests assert the fix rather than reproduce the contention (a live lock-
race is inherently timing-dependent and would be flaky) — WAL mode is on, and
a reader genuinely does not block behind an open writer transaction.
"""

import sqlite3
import threading
import time

import pytest


def test_registry_conn_sets_wal_and_busy_timeout(tmp_path, monkeypatch):
    from app.core import registry
    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "t.db")
    c = registry._conn()
    try:
        assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000
    finally:
        c.close()


def test_token_manager_conn_sets_wal_and_busy_timeout(tmp_path, monkeypatch):
    from app.core import token_manager
    monkeypatch.setattr(token_manager, "DB_PATH", tmp_path / "t.db")
    c = token_manager._conn()
    try:
        assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000
    finally:
        c.close()


def test_both_modules_share_wal_on_the_same_file(tmp_path, monkeypatch):
    """The two modules open the SAME optionslab.db independently — assert one
    doesn't set WAL while the other reverts it (they'd fight, and the loser
    depends on connection order, hiding the bug until it randomly returns)."""
    from app.core import registry, token_manager
    db = tmp_path / "shared.db"
    monkeypatch.setattr(registry, "DB_PATH", db)
    monkeypatch.setattr(token_manager, "DB_PATH", db)
    registry._conn().close()
    c = token_manager._conn()
    try:
        assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        c.close()


def test_a_reader_does_not_block_behind_an_open_writer(tmp_path, monkeypatch):
    """The actual symptom, reproduced: with WAL, a SELECT must return WHILE
    another connection holds an open (uncommitted) write transaction — the
    default rollback-journal mode would hang this reader until the writer's
    transaction ends."""
    from app.core import registry
    monkeypatch.setattr(registry, "DB_PATH", tmp_path / "t.db")
    setup = registry._conn()
    setup.execute("CREATE TABLE x (v INTEGER)")
    setup.execute("INSERT INTO x VALUES (1)")
    setup.commit()
    setup.close()

    writer = registry._conn()
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE x SET v = 2")   # open, uncommitted

    result = {}

    def read():
        reader = registry._conn()
        try:
            result["v"] = reader.execute("SELECT v FROM x").fetchone()[0]
        finally:
            reader.close()

    t = threading.Thread(target=read)
    start = time.monotonic()
    t.start()
    t.join(timeout=2.0)
    elapsed = time.monotonic() - start
    writer.rollback()
    writer.close()

    assert not t.is_alive(), (
        "reader was still blocked after 2s behind an open writer — WAL isn't "
        "actually preventing the reader/writer stall this fix targets")
    assert elapsed < 1.0, f"reader took {elapsed:.2f}s to return (should be near-instant)"
    assert result.get("v") == 1, "reader must see the pre-transaction value"
