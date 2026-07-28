"""No long-running task may die silently.

2026-07-28: the chain poll loop died on an unguarded statement and took chain
recording with it — no restart, no event, and every other indicator green. An
audit found the same hole in five sibling tasks, each with a worse blast
radius than the last:

  token_manager.daily_refresh_loop  no 08:30 check -> token expires next
                                    morning -> everything fails for a day
  MarketHub._eod_clock              no 15:31 flush -> no square-off, no
                                    persisted day
  PaperRunner._heartbeat            M4 snapshots stop -> a restart loses
                                    open positions
  PaperRunner._mtm_refresh_loop     displayed P&L and stop/target checks
                                    freeze
  PaperRunner._loop                 on_bar was guarded, but on_start and
                                    on_day_end (also strategy code) were not

This test is structural: it parses the source and asserts every such loop
body sits inside a try. A behavioural test per loop would need a live event
loop and would drift; the invariant is "no unguarded statement in the body",
and that is exactly what the parser can check.
"""

import ast
import pathlib

import pytest

# (file, function) for every task that runs for the life of the process.
LONG_RUNNING = [
    ("app/engines/paper.py", "_watchdog_loop"),
    ("app/engines/paper.py", "_index_vol_check_loop"),
    ("app/engines/paper.py", "_eod_clock"),
    ("app/engines/paper.py", "_chain_poll_loop"),
    ("app/engines/paper.py", "_mtm_refresh_loop"),
    ("app/engines/paper.py", "_heartbeat"),
    ("app/engines/paper.py", "_loop"),
    ("app/core/token_manager.py", "daily_refresh_loop"),
]

# Statements that are safe outside the try: `await asyncio.sleep(...)` (only
# raises CancelledError, which MUST propagate so shutdown works) and loop
# bookkeeping like `cycle += 1`.
SAFE_OUTSIDE = (ast.Expr, ast.AugAssign, ast.Pass)


def _while_bodies(path: str, func: str):
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) \
                and node.name == func:
            for stmt in node.body:
                if isinstance(stmt, ast.While):
                    yield stmt
            return
    pytest.fail(f"{func} not found in {path}")


@pytest.mark.parametrize("path,func", LONG_RUNNING)
def test_loop_body_is_guarded(path, func):
    found = False
    for w in _while_bodies(path, func):
        found = True
        unguarded = [s for s in w.body
                     if not isinstance(s, (ast.Try,) + SAFE_OUTSIDE)]
        assert not unguarded, (
            f"{path}:{func} has {len(unguarded)} statement(s) outside a try in "
            f"its loop body ({[type(s).__name__ for s in unguarded]}). An "
            f"exception there kills the task silently — see this module's "
            f"docstring.")
        assert any(isinstance(s, ast.Try) for s in w.body), (
            f"{path}:{func} loop body has no try at all")
    assert found, f"no while loop found in {func}"


def test_strategy_hooks_all_run_through_the_guard():
    """Invariant #6: a crashing strategy auto-pauses, never kills the loop.
    on_bar was guarded while on_start/on_day_end were not, so the guard is now
    a shared helper — assert every hook call goes through it."""
    src = pathlib.Path("app/engines/paper.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    runner_loop = next(n for n in ast.walk(tree)
                       if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                       and n.name == "_loop")
    direct = []
    for node in ast.walk(runner_loop):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "strategy"):
                direct.append(node.func.attr)
    assert direct == [], (
        f"strategy hook(s) {direct} called directly in _loop — route them "
        f"through _guard_strategy so a crash auto-pauses instead of killing "
        f"the task")


def test_guard_helper_exists_and_pauses():
    """The helper must actually pause, not just swallow."""
    src = pathlib.Path("app/engines/paper.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
               and n.name == "_guard_strategy"), None)
    assert fn is not None, "_guard_strategy helper is missing"
    body = ast.dump(fn)
    assert "set_paused" in body, "_guard_strategy must pause the strategy"
    assert "DEPLOYED_PAUSED" in body, "_guard_strategy must transition state"


# --- crash-loop containment -------------------------------------------------

class _FakeCtx:
    def __init__(self):
        self.logs = []
        self.paused = False

    def log(self, m):
        self.logs.append(m)

    def set_paused(self, v):
        self.paused = v


def _runner(monkeypatch):
    from app.engines.paper import PaperRunner
    r = PaperRunner.__new__(PaperRunner)
    events = []
    monkeypatch.setattr("app.core.registry.record_event",
                        lambda *a, **k: events.append(a))
    monkeypatch.setattr("app.core.registry.transition", lambda *a, **k: None)
    return r, events


def test_repeated_crashes_disable_the_hook(monkeypatch):
    """Pause keeps delivering on_bar so exits stay managed — so a strategy
    crashing inside its own manage block re-crashes every bar forever. Three
    strikes and we stop calling it."""
    r, events = _runner(monkeypatch)
    ctx = _FakeCtx()
    calls = []

    def boom():
        calls.append(1)
        raise ValueError("float - None")

    for _ in range(10):
        r._guard_strategy("sid1", ctx, "on_bar", boom)
    assert len(calls) == r.MAX_HOOK_CRASHES, "hook kept being called after giving up"
    assert ctx.paused is True
    assert any("DISABLED" in str(a) for a in events)


def test_a_success_resets_the_crash_counter(monkeypatch):
    """A transient failure must not burn down the budget permanently."""
    r, _ = _runner(monkeypatch)
    ctx = _FakeCtx()
    state = {"fail": True}
    calls = []

    def flaky():
        calls.append(1)
        if state["fail"]:
            raise ValueError("transient")

    r._guard_strategy("sid2", ctx, "on_bar", flaky)
    r._guard_strategy("sid2", ctx, "on_bar", flaky)
    state["fail"] = False
    r._guard_strategy("sid2", ctx, "on_bar", flaky)   # recovers, resets
    state["fail"] = True
    for _ in range(5):
        r._guard_strategy("sid2", ctx, "on_bar", flaky)
    # 2 fails + 1 success + 3 more fails before giving up again
    assert len(calls) == 6


def test_hooks_are_tracked_independently(monkeypatch):
    """on_bar giving up must not silence on_day_end."""
    r, _ = _runner(monkeypatch)
    ctx = _FakeCtx()
    bar_calls, day_calls = [], []

    def bad_bar():
        bar_calls.append(1)
        raise ValueError("x")

    def good_day():
        day_calls.append(1)

    for _ in range(6):
        r._guard_strategy("sid3", ctx, "on_bar", bad_bar)
        r._guard_strategy("sid3", ctx, "on_day_end", good_day)
    assert len(bar_calls) == r.MAX_HOOK_CRASHES
    assert len(day_calls) == 6


def test_strategies_are_tracked_independently(monkeypatch):
    """One broken strategy must not disable another's hook."""
    r, _ = _runner(monkeypatch)
    ctx = _FakeCtx()
    a, b = [], []

    def bad_a():
        a.append(1)
        raise ValueError("x")

    def good_b():
        b.append(1)

    for _ in range(6):
        r._guard_strategy("A", ctx, "on_bar", bad_a)
        r._guard_strategy("B", ctx, "on_bar", good_b)
    assert len(a) == r.MAX_HOOK_CRASHES
    assert len(b) == 6
