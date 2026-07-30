"""Session-wide test isolation: no test may write to the real optionslab.db.

2026-07-30: a pre-existing, previously-safe test
(test_chain.py::test_fetch_chain_returns_none_on_persistent_empty_failure)
started writing a real "SOMESTOCK" event row into the PRODUCTION events
table on every deploy, the moment a code change added a
registry.record_event() call inside a function that test exercises. The
test file never mentions "registry" anywhere — the write happens
transitively, several calls deep, inside app code — so auditing test
isolation by grepping test files for "registry." is not reliable: it
cannot see writes that only happen through application code a test invokes.

Individual tests have mostly been diligent about redirecting
registry.DB_PATH themselves (see the many `monkeypatch.setattr(registry,
"DB_PATH", ...)` calls across the suite). But "remember to redirect it in
every test, forever, including ones that don't obviously touch the
database" is exactly the kind of discipline that silently lapses — as this
incident proved, on the very next code change after the pattern was
established.

This makes it structural instead: EVERY test gets its own throwaway sqlite
file for registry.DB_PATH and token_manager.DB_PATH before it runs — they
are the SAME file in production, so they stay the same file here. A test
that wants a specific temp path of its own (e.g. to inspect the file
directly) can still monkeypatch over this default; this only removes the
silent DEFAULT path to the real one.

Deliberately NOT extended to app.data.store.DB_PATH (DuckDB): DataStore
binds its default `path=DB_PATH` argument at import time, not call time, so
patching the module attribute here would not protect a bare `DataStore()`
call the way it does for the SQLite side — a test relying on that would
need `monkeypatch.setattr(DataStore, "__init__", ...)` or an explicit path
argument, not this fixture. See CLAUDE.md for the audit status of that
side.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolated_sqlite_db(tmp_path, monkeypatch):
    from app.core import registry, token_manager

    db = tmp_path / "test_optionslab.db"
    monkeypatch.setattr(registry, "DB_PATH", db)
    monkeypatch.setattr(token_manager, "DB_PATH", db)
    registry.init_db()   # schema must exist before any incidental write —
                         # a test that writes without ever reading first
                         # (e.g. a bare record_event() call) would otherwise
                         # hit "no such table: events" against the fresh file
    yield
