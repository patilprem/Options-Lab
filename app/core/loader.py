"""
Strategy Loader & Validator
===========================
Takes raw pasted code (from an LLM), and in order:

  1. AST scan   — reject dangerous imports / builtins (os, socket, eval...)
  2. Compile    — inside a namespace that already contains the contract
                  types, so generated code doesn't even need imports.
  3. Discover   — find exactly one Strategy subclass.
  4. Smoke test — instantiate, read meta(), run on_bar() against a tiny
                  synthetic context to catch crashes before deployment.

NOTE: AST filtering is a guardrail, not a true security sandbox. Since
you are the only user pasting code you asked an LLM to write, that is an
acceptable trade-off. If you ever open this to other users, run each
strategy in a separate OS process with seccomp / no network instead.
"""

from __future__ import annotations

import ast
import traceback
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Optional

from app.core import contract as C

BANNED_IMPORTS = {
    "os", "sys", "subprocess", "socket", "requests", "urllib", "http",
    "shutil", "pathlib", "importlib", "ctypes", "multiprocessing",
    "threading", "asyncio", "pickle", "marshal", "builtins", "open",
}
ALLOWED_IMPORTS = {
    "math", "statistics", "datetime", "dataclasses", "typing", "enum",
    "collections", "itertools", "functools", "random", "numpy", "pandas",
}
BANNED_CALLS = {"eval", "exec", "compile", "open", "__import__", "input", "globals", "vars"}


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str]
    warnings: list[str]
    meta: Optional[C.StrategyMeta] = None
    strategy_class_name: Optional[str] = None


def _scan_ast(code: str) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"Syntax error: line {e.lineno}: {e.msg}"], []

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) \
                else [(node.module or "").split(".")[0]]
            for name in names:
                if name in BANNED_IMPORTS:
                    errors.append(f"Banned import: '{name}' (line {node.lineno})")
                elif name and name not in ALLOWED_IMPORTS and name != "app":
                    warnings.append(f"Unusual import '{name}' (line {node.lineno}) — not in whitelist")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in BANNED_CALLS:
                errors.append(f"Banned call: '{fn.id}()' (line {node.lineno})")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr not in ("__init__", "__post_init__"):
                errors.append(f"Dunder attribute access '{node.attr}' not allowed (line {node.lineno})")
    return errors, warnings


def _exec_namespace() -> dict:
    """Namespace pre-loaded with the contract so LLM code can use the
    types directly (Strategy, LegSpec, OptionType...) without imports."""
    ns = {"__builtins__": {
        n: __builtins__[n] if isinstance(__builtins__, dict) else getattr(__builtins__, n)
        for n in (
            "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float",
            "int", "isinstance", "len", "list", "map", "max", "min", "print",
            "range", "round", "set", "sorted", "str", "sum", "tuple", "zip",
            "Exception", "ValueError", "TypeError", "KeyError", "property",
            "staticmethod", "classmethod", "super", "type", "object",
            "__build_class__",
        )
    }}
    ns["__name__"] = "strategy"
    for name in ("Strategy", "Context", "StrategyMeta", "LegSpec", "Bar",
                 "OptionQuote", "Position", "OptionType", "Action", "ExpiryKind"):
        ns[name] = getattr(C, name)
    return ns


class _SmokeContext(C.Context):
    """Minimal fake context used only to smoke-test on_bar()."""

    def __init__(self):
        self._now = datetime(2025, 1, 6, 9, 30)
        self._bars = [
            C.Bar(self._now - timedelta(minutes=5 * (20 - i)),
                  22000 + i, 22010 + i, 21990 + i, 22005 + i, 1000, 0)
            for i in range(20)
        ]
        self.calls: list[str] = []

    @property
    def now(self): return self._now
    @property
    def spot(self): return self._bars[-1].close

    def option(self, leg):
        strike = round(self.spot / 50) * 50 + leg.strike_offset * 50
        return C.OptionQuote(self._now, "SMOKE", date(2025, 1, 9), strike,
                             leg.option_type, 100.0, 99.5, 100.5, 15.0, 1e5, 1e4,
                             0.5, -5.0, 10.0, 0.001)

    def history(self, n): return self._bars[-n:]
    def signal(self, name): return None   # no scanner in the smoke harness (F6)
    @property
    def positions(self): return []
    @property
    def allocated_capital(self): return 1_000_000.0
    @property
    def available_capital(self): return 1_000_000.0
    @property
    def day_pnl(self): return 0.0

    def enter(self, legs, tag="", sl_pct=None, target_pct=None):
        self.calls.append(f"enter({len(legs)} legs, tag={tag!r})")
        return True

    def set_levels(self, position_id, stop_loss=None, target=None):
        self.calls.append(f"set_levels({position_id})")
        return True

    def exit(self, position_id):
        self.calls.append(f"exit({position_id})")
        return True

    def exit_all(self): self.calls.append("exit_all()")
    def log(self, msg): self.calls.append(f"log: {msg}")


def load_strategy_class(code: str):
    """Compile validated code and return the Strategy subclass."""
    ns = _exec_namespace()
    exec(compile(code, "<strategy>", "exec"), ns)  # noqa: S102 (validated above)
    classes = [v for v in ns.values()
               if isinstance(v, type) and issubclass(v, C.Strategy) and v is not C.Strategy]
    if len(classes) != 1:
        raise ValueError(f"Expected exactly 1 Strategy subclass, found {len(classes)}")
    return classes[0]


def validate(code: str) -> ValidationResult:
    errors, warnings = _scan_ast(code)
    if errors:
        return ValidationResult(False, errors, warnings)

    try:
        cls = load_strategy_class(code)
    except Exception as e:
        return ValidationResult(False, [f"Load failed: {e}"], warnings)

    try:
        strat = cls()
        meta = strat.meta()
        assert isinstance(meta, C.StrategyMeta), "meta() must return StrategyMeta"
        smoke = _SmokeContext()
        strat.on_start(smoke)
        for bar in smoke.history(5):
            strat.on_bar(smoke, bar)
        strat.on_day_end(smoke)
        strat.on_stop(smoke)
    except Exception:
        return ValidationResult(False, [f"Smoke test crashed:\n{traceback.format_exc(limit=3)}"], warnings)

    return ValidationResult(True, [], warnings, meta=meta, strategy_class_name=cls.__name__)
