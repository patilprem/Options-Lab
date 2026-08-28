"""
Would the new entry checklist have taken my past trades?
========================================================
    venv/bin/python -m scripts.entry_gate_replay            # on the VPS

Replays the auto-trader's CURRENT entry gates — the confluence profile
(volume surge, liquidity, range alignment, index bias, spread) AND the
technical-read checklist (VWAP side, EMA trend, break-and-hold, RSI,
distance-from-VWAP, structural R:R) plus the day-level circuit breakers —
over every closed round trip in scanner_journal, and reports which trades
would have been KEPT vs BLOCKED and the P&L of each.

METHOD, and what makes it honest:
- Gate logic is IMPORTED from app.engines.scanner_trader (entry_quality),
  never restated, so the replay and the live trader cannot drift (the
  mcx_audit/mcx_purge shared-rule precedent).
- Old journal rows never recorded a technical read — it is RECOMPUTED
  point-in-time from the stock_snapshots series (engines/tech_read.py),
  truncated at each trade's entry_ts so no bar after the entry can leak in.
- liquidity_ok was never journaled; it is PROXIED from the recorded
  worst_spread_pct (<= 2.0%, the liquidity_screen default, = vetted ok).
  The report says so.
- Fields that simply did not exist when a trade was taken (range_pos and
  volume_surge before ~07-27, snapshot series before the scanner ran, the
  opening 20 minutes of a session) fail the gates CLOSED, exactly as live —
  but the report separates "blocked by an unfavorable reading" from
  "blocked because the data wasn't recorded yet", and the policy table
  shows both treatments, so the headline is never an instrumentation
  artifact.

Also breaks down the biggest first-failing-gate bucket (usually entry_score,
since it's checked first): of the trades it blocked, how many would ALSO
have failed the newer checklist independently (real, additional work) vs
how many the score bar alone would have caught (see gates_failed_independently
/ score_gate_overlap — each candidate is re-run through entry_quality with
ONE gate armed at a time, so no gate's condition is ever restated).

WHAT IT CANNOT DO (also printed): price trades the new system would have
taken that the old one didn't (premiums are only recorded for names
actually held), or escape the in-sample caveat — these gates were designed
partly from these same losing trades, so a flattering number is expected.
The live 'entry gated' events (since 2026-08-28) are the forward test.

READ-ONLY BY CONSTRUCTION: both databases are read from temp COPIES
(the service holds DuckDB's exclusive lock and owns the WAL sqlite;
scripts/diag.py / mcx_contract_audit precedent). Never uses get_store() —
its SyntheticStore fallback would silently report 100%% blocked.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = os.environ.get("OPTIONSLAB_ROOT", "/opt/optionslab")

# liquidity_screen's default max_spread_pct — the proxy threshold for
# "this chain would have passed the screen" from the journaled worst spread.
LIQ_SPREAD_OK_PCT = 2.0

# reason substrings that mean "the data wasn't there", not "the data said no"
_MISSING_MARKERS = ("unknown", "no technical read", "warming up",
                    "no prev-day", "not deep-dived")

_GATE_LABELS = (
    ("no technical read", "tech_missing"),
    ("trade cap", "trade_cap"), ("daily-loss", "daily_loss_stop"),
    ("spread", "spread"),
    ("wrong side of VWAP", "vwap_side"), ("from VWAP", "vwap_dist"),
    ("VWAP unknown", "vwap_side"),
    ("trend", "trend_align"), ("break-and-hold", "structure_break"),
    ("RSI", "rsi"), ("R:R", "risk_reward"),
    ("target/stop", "risk_reward"), ("prev-day", "risk_reward"),
    ("liquidity", "liquid_chain"), ("deep-dived", "liquid_chain"),
    ("volume", "volume_surge"), ("range", "range_align"),
    ("day-range", "range_align"), ("market bias", "index_align"),
    ("score", "entry_score"), ("fuel", "fresh_buildup"),
    ("bias", "no_bias"),
)


def gate_label(reason: str | None) -> str:
    for marker, label in _GATE_LABELS:
        if reason and marker in reason:
            return label
    return "other"


def is_data_missing(reason: str | None) -> bool:
    return bool(reason) and any(m in reason for m in _MISSING_MARKERS)


# ---------------------------------------------------------------------------
# Pure replay core (tests drive these directly)
# ---------------------------------------------------------------------------

def build_candidate(row: dict, tech: dict | None) -> dict:
    """What ranked_scores() would have shown for this trade at entry time:
    the journaled entry context reshaped into the setup_score dict
    entry_quality() reads. `bias` lives at the row top level in the journal;
    liquidity_ok is proxied from the recorded chain-wide worst spread."""
    ctx = row.get("entry_ctx") or {}
    ws = ctx.get("worst_spread_pct")
    liquidity_ok = None if ws is None else bool(ws <= LIQ_SPREAD_OK_PCT)
    sc = {
        "symbol": row.get("symbol"),
        "score": row.get("entry_score") or ctx.get("score"),
        "bias": row.get("bias"),
        "buildup": ctx.get("buildup"),
        "volume_surge": ctx.get("volume_surge"),
        "range_pos": ctx.get("range_pos"),
        "liquidity_ok": liquidity_ok,
    }
    if tech is not None:
        sc["tech"] = tech
    return sc


def reconstruct_tech(store, trades: list[dict]) -> dict:
    """{(symbol, entry_ts): technical_read dict} for every trade, recomputed
    POINT-IN-TIME: the day's snapshot series truncated at entry_ts, prev-day
    levels from strictly before the entry date. Two bulk store queries per
    trade date (never a per-symbol loop — the store's 30s-stall lesson;
    harmless here anyway, since we query a temp copy)."""
    from app.engines import tech_read as TR
    by_day: dict[str, set] = {}
    entry_dt: dict[tuple, datetime] = {}
    for t in trades:
        ts = _entry_dt(t)
        if ts is None or not t.get("symbol"):
            continue
        key = (t["symbol"], t.get("entry_ts"))
        entry_dt[key] = ts
        by_day.setdefault(ts.date().isoformat(), set()).add(t["symbol"])

    out: dict[tuple, dict] = {}
    for day, symbols in sorted(by_day.items()):
        syms = sorted(symbols)
        series = store.stock_day_series_bulk(syms, day)
        prev = store.stock_prev_day_levels_bulk(syms, day)
        for (sym, ets_iso), ets in entry_dt.items():
            if ets.date().isoformat() != day:
                continue
            rows = [r for r in (series.get(sym) or []) if r[0] <= ets]
            out[(sym, ets_iso)] = TR.technical_read(
                TR.snapshot_rows_to_bars(rows), prev.get(sym))
    return out


def _entry_dt(row: dict):
    try:
        return datetime.fromisoformat((row.get("entry_ts") or "")
                                      .replace("Z", ""))
    except (ValueError, TypeError):
        return None


def _exit_dt(row: dict):
    raw = row.get("ts") or row.get("exit_ts") or ""
    try:
        return datetime.fromisoformat(raw.replace("Z", ""))
    except (ValueError, TypeError):
        return None


def judge_trades(exits: list[dict], tech_by_key: dict, cfg) -> list[dict]:
    """Per-trade signal verdict (no day-breakers yet): run entry_quality on
    the reconstructed candidate, then the fill-time spread gate exactly as
    step() applies it. Returns one dict per trade, entry-ts ascending."""
    from app.engines.scanner_trader import entry_quality
    verdicts = []
    for row in sorted(exits, key=lambda r: r.get("entry_ts") or ""):
        ctx = row.get("entry_ctx") or {}
        tech = tech_by_key.get((row.get("symbol"), row.get("entry_ts")))
        sc = build_candidate(row, tech)
        ok, why = entry_quality(sc, cfg, market_bias=ctx.get("market_bias"))
        if ok and cfg.max_entry_spread_pct:
            sp = ctx.get("opt_spread_pct")
            if sp is None:
                ok, why = False, "contract spread unknown (one-sided quote)"
            elif sp > cfg.max_entry_spread_pct:
                ok, why = False, (f"spread {sp:.1f}% exceeds the "
                                  f"{cfg.max_entry_spread_pct:g}% entry cap")
        verdicts.append({
            "symbol": row.get("symbol"), "bias": row.get("bias"),
            "entry_ts": row.get("entry_ts"), "exit_ts": row.get("ts"),
            "realized": row.get("realized") or 0.0,
            "reason_out": row.get("reason"),
            "passed": ok, "why": why,
            "gate": None if ok else gate_label(why),
            "data_missing": (not ok) and is_data_missing(why),
            "candidate": sc, "market_bias": ctx.get("market_bias"),
        })
    return verdicts


# Every optional gate entry_quality can check, as (label, TradeConfig field,
# the value to arm it at if the live config currently has it off) — shared
# by gates_failed_independently so isolating one gate never restates what
# "armed" means for it.
_TECH_GATES = (
    ("vwap_side", "require_vwap_side", 1),
    ("trend_align", "require_trend_align", 1),
    ("structure_break", "require_structure_break", 1),
    ("rsi", "max_rsi_extreme", 75.0),
    ("vwap_dist", "max_vwap_dist_pct", 2.5),
    ("risk_reward", "min_rr", 1.5),
)
_NON_TECH_GATES = (
    ("fresh_buildup", "fresh_buildup_only", 1),
    ("volume_surge", "min_volume_surge", 1.5),
    ("liquid_chain", "require_liquid_chain", 1),
    ("range_align", "min_range_align", 0.6),
    ("index_align", "index_align", 1),
)
_ALL_TOGGLE_FIELDS = dict(
    entry_score=0, fresh_buildup_only=0, min_volume_surge=0,
    require_liquid_chain=0, min_range_align=0, index_align=0,
    require_vwap_side=0, require_trend_align=0, require_structure_break=0,
    max_rsi_extreme=0, max_vwap_dist_pct=0, min_rr=0)


def gates_failed_independently(sc: dict, cfg, market_bias=None) -> set:
    """Every gate this candidate fails ON ITS OWN, tested one knob at a
    time — unlike entry_quality's short-circuit (which stops at the FIRST
    failure and hides whether the trade would also have failed anything
    else). Each check re-runs the REAL entry_quality with every other knob
    switched off, so no gate's logic is restated here.

    entry_score is judged directly (it isn't a 0/off-able knob — raising it
    is the only way to disable it, which isn't a meaningful 'off' for this
    isolation test). Missing technical data collapses to one 'tech_missing'
    entry rather than six: without a read, VWAP/trend/structure/RSI/R:R
    can't be separately judged, so listing all six would double-count one
    problem as if it were six independent ones."""
    from dataclasses import replace as _replace

    from app.engines.scanner_trader import entry_quality
    if not sc.get("bias"):
        return {"no_bias"}
    failed: set = set()
    if (sc.get("score") or 0) < cfg.entry_score:
        failed.add("entry_score")

    def _armed(field, fallback):
        cur = getattr(cfg, field)
        return cur if cur else fallback

    def _check(label, field, fallback):
        c = _replace(cfg, **{**_ALL_TOGGLE_FIELDS, field: _armed(field, fallback)})
        ok, _why = entry_quality(sc, c, market_bias)
        if not ok:
            failed.add(label)

    for label, field, fallback in _NON_TECH_GATES:
        _check(label, field, fallback)

    tech = sc.get("tech")
    if not tech or not tech.get("n_bars"):
        failed.add("tech_missing")
    else:
        for label, field, fallback in _TECH_GATES:
            _check(label, field, fallback)

    return failed


def score_gate_overlap(verdicts: list, cfg) -> dict:
    """For every trade whose FIRST failing gate was entry_score: would it
    ALSO independently fail something else, or was the raised score bar the
    ONLY thing standing between it and the ledger? Answers the natural
    follow-up to a big entry_score count in the attribution table — how
    much of that number is really 'the score' vs 'everything else, too'."""
    scored_out = [v for v in verdicts if v["gate"] == "entry_score"]
    only_score, also_other = [], []
    other_counts: dict = {}
    for v in scored_out:
        failed = gates_failed_independently(
            v.get("candidate") or {}, cfg, market_bias=v.get("market_bias"))
        others = failed - {"entry_score"}
        if others:
            also_other.append(v)
            for g in others:
                other_counts[g] = other_counts.get(g, 0) + 1
        else:
            only_score.append(v)
    return {"n": len(scored_out), "only_score": _stats(only_score),
            "also_other": _stats(also_other),
            "other_gate_counts": dict(sorted(other_counts.items(),
                                             key=lambda kv: -kv[1]))}


def apply_breakers(verdicts: list[dict], cfg, keep_missing: bool = False) -> set:
    """Indices (into `verdicts`) that ALSO survive the day-level circuit
    breakers, applied sequentially in entry order over the kept book:
    max_trades_per_day counts kept entries; daily_loss_stop_pct counts
    realized P&L of kept trades whose EXIT landed earlier the same day.
    Blocked trades get their breaker reason written back onto the verdict."""
    surviving: set[int] = set()
    kept_rows: list[dict] = []
    for i, v in enumerate(verdicts):
        if not (v["passed"] or (keep_missing and v["data_missing"])):
            continue
        ets = _entry_dt(v)
        day = ets.date() if ets else None
        day_kept = [k for k in kept_rows
                    if (_entry_dt(k) and day and _entry_dt(k).date() == day)]
        if cfg.max_trades_per_day and len(day_kept) >= cfg.max_trades_per_day:
            v["breaker"] = "trade cap"
            continue
        if cfg.daily_loss_stop_pct and ets is not None:
            realized_so_far = 0.0
            for k in kept_rows:
                xts = _exit_dt(k)
                if xts and day and xts.date() == day and xts <= ets:
                    realized_so_far += k["realized"]
            if realized_so_far <= -abs(cfg.daily_loss_stop_pct) * cfg.capital:
                v["breaker"] = "daily-loss stop"
                continue
        surviving.add(i)
        kept_rows.append(v)
    return surviving


def _stats(trades: list[dict]) -> dict:
    pnls = [t["realized"] for t in trades]
    wins = [p for p in pnls if p > 0]
    return {"n": len(trades), "net": sum(pnls),
            "win_rate": (len(wins) / len(pnls)) if pnls else None}


def summarize(verdicts: list[dict], cfg) -> dict:
    """The policy table + attribution the report prints. Kept/blocked P&L
    always sums back to the actual baseline — nothing is invented."""
    actual = _stats(verdicts)
    signal_kept = [v for v in verdicts if v["passed"] or v["data_missing"]]
    strict_kept = [v for v in verdicts if v["passed"]]
    full_idx = apply_breakers(verdicts, cfg, keep_missing=False)
    full_kept = [v for i, v in enumerate(verdicts) if i in full_idx]

    blocked = [v for v in verdicts if not v["passed"]]
    by_gate: dict[str, dict] = {}
    for v in blocked:
        g = by_gate.setdefault(v["gate"], {"n": 0, "net": 0.0})
        g["n"] += 1
        g["net"] += v["realized"]
    # breaker-blocked trades passed the signal gates but were stopped by a
    # day-level circuit breaker — attribute them too (apply_breakers wrote
    # the reason back onto the verdict)
    for v in verdicts:
        if v["passed"] and v.get("breaker"):
            g = by_gate.setdefault(gate_label(v["breaker"]),
                                   {"n": 0, "net": 0.0})
            g["n"] += 1
            g["net"] += v["realized"]
    return {
        "actual": actual,
        "policies": [
            ("Actual (all past trades)", actual),
            ("Signal gates only (missing data forgiven)", _stats(signal_kept)),
            ("Full checklist (missing data = blocked, as live)",
             _stats(strict_kept)),
            ("Full checklist + day circuit breakers", _stats(full_kept)),
        ],
        "kept": _stats(full_kept),
        "blocked": _stats([v for i, v in enumerate(verdicts)
                           if i not in full_idx]),
        "by_gate": dict(sorted(by_gate.items(),
                               key=lambda kv: kv[1]["net"])),
        "missing": _stats([v for v in blocked if v["data_missing"]]),
        "missing_n": sum(1 for v in blocked if v["data_missing"]),
    }


# ---------------------------------------------------------------------------
# I/O: temp copies of the live DBs (read-only by construction)
# ---------------------------------------------------------------------------

def open_registry_copy(root: str = ROOT):
    """Repoint app.core.registry at a copied optionslab.db (+ WAL sidecars)
    so journal_rows()/setting() work unchanged without touching, or needing
    write access to, the live file. Returns the tempdir (caller cleans up)."""
    from app.core import registry
    src = os.path.join(root, "optionslab.db")
    if not os.path.exists(src):
        raise FileNotFoundError(f"no registry DB at {src} "
                                "(set OPTIONSLAB_ROOT or --root)")
    tmp = tempfile.mkdtemp(prefix="olab-gate-replay-reg-")
    for f in glob.glob(src + "*"):            # .db, -wal, -shm
        shutil.copy(f, tmp)
    registry.DB_PATH = Path(tmp) / "optionslab.db"
    return tmp


def open_store_copy(root: str = ROOT):
    """A real DataStore over a copied marketdata.duckdb. NEVER get_store():
    with the service holding DuckDB's exclusive lock it silently falls back
    to SyntheticStore, whose empty stock-series stubs would make this replay
    report 100% blocked — a plausible-looking wrong answer."""
    src = os.path.join(root, "marketdata.duckdb")
    if not os.path.exists(src):
        raise FileNotFoundError(f"no market-data store at {src} "
                                "(set OPTIONSLAB_ROOT or --root)")
    tmp = tempfile.mkdtemp(prefix="olab-gate-replay-duck-")
    for f in glob.glob(src + "*"):
        shutil.copy(f, tmp)
    from app.data.store import DataStore
    return DataStore(Path(tmp) / "marketdata.duckdb"), tmp


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _pct(x):
    return "—" if x is None else f"{x * 100:.0f}%"


def print_report(summary: dict, verdicts: list[dict], cfg,
                 overlap: dict | None = None, verbose: bool = False) -> None:
    def section(title):
        print(f"\n=== {title} ===")

    a = summary["actual"]
    section("Baseline — the book as it actually traded")
    days = sorted({v["entry_ts"][:10] for v in verdicts if v["entry_ts"]})
    span = f"{days[0]} .. {days[-1]}" if days else "—"
    print(f"{a['n']} closed trades over {span} · net ₹{a['net']:,.0f} · "
          f"win rate {_pct(a['win_rate'])}")

    section("Policy comparison (Δ vs actual)")
    print(f"{'policy':<50}{'n':>5}{'net ₹':>12}{'Δ ₹':>12}{'win':>6}")
    for name, s in summary["policies"]:
        print(f"{name:<50}{s['n']:>5}{s['net']:>12,.0f}"
              f"{s['net'] - a['net']:>12,.0f}{_pct(s['win_rate']):>6}")

    k, b = summary["kept"], summary["blocked"]
    section("Kept vs blocked (full checklist + breakers)")
    print(f"kept    {k['n']:>4} trades · net ₹{k['net']:>10,.0f} · "
          f"win {_pct(k['win_rate'])}")
    print(f"blocked {b['n']:>4} trades · net ₹{b['net']:>10,.0f} · "
          f"win {_pct(b['win_rate'])}  (negative = losses avoided)")

    section("What blocked them (first failing gate)")
    print(f"{'gate':<18}{'trades':>7}{'P&L it avoided ₹':>18}")
    for gate, g in summary["by_gate"].items():
        print(f"{gate:<18}{g['n']:>7}{g['net']:>18,.0f}")
    m = summary["missing"]
    if m["n"]:
        print(f"\n  of the blocked, {m['n']} were blocked for MISSING DATA "
              f"(field not recorded yet / series absent), net ₹{m['net']:,.0f}"
              " — see the 'missing data forgiven' policy row for the "
              "signal-only effect.")

    if overlap and overlap["n"]:
        os_, ao = overlap["only_score"], overlap["also_other"]
        section(f"entry_score overlap — of the {overlap['n']} trades blocked "
               "first by score, would the rest ALSO have blocked them?")
        print(f"  score was the ONLY problem: {os_['n']} trades · "
              f"net ₹{os_['net']:,.0f} · win {_pct(os_['win_rate'])}  "
              "(a bare score bump alone would have let these in)")
        print(f"  would ALSO fail >=1 other gate independently: "
              f"{ao['n']} trades · net ₹{ao['net']:,.0f} · "
              f"win {_pct(ao['win_rate'])}  "
              "(the newer checklist is doing real work here, not just the "
              "higher bar)")
        if overlap["other_gate_counts"]:
            print("  which other gate(s), among those "
                  f"{ao['n']} (a trade can fail more than one):")
            for g, n in overlap["other_gate_counts"].items():
                print(f"    {g:<18}{n:>5}")

    if verbose:
        section("Per-trade verdicts")
        print(f"{'entry':<17}{'symbol':<12}{'bias':<5}{'realized ₹':>11}  verdict")
        for v in verdicts:
            tag = ("KEPT" if v["passed"] and "breaker" not in v
                   else f"BLOCKED[{v.get('breaker') or v['gate']}]")
            why = "" if v["passed"] and "breaker" not in v else \
                f" — {v.get('breaker') or v['why']}"
            print(f"{(v['entry_ts'] or '')[:16]:<17}{v['symbol']:<12}"
                  f"{v['bias'] or '—':<5}{v['realized']:>11,.0f}  {tag}{why}")

    section("Read this honestly")
    print(
        "* IN-SAMPLE: these gates were designed partly from these same\n"
        "  trades — a flattering number here is expected, not proof. The\n"
        "  forward test is the live 'entry gated' events + the shadow\n"
        "  challenger, both running since 2026-08-28.\n"
        "* ONE-SIDED: this replays only trades actually taken. Trades the\n"
        "  checklist would have taken INSTEAD cannot be priced (no premium\n"
        "  series is recorded for names never held).\n"
        "* liquidity_ok was never journaled — proxied from the recorded\n"
        f"  chain worst-spread (<= {LIQ_SPREAD_OK_PCT:g}% = ok).\n"
        "* Gates replayed at the CURRENT settings "
        f"(entry_score {cfg.entry_score:g}, min_rr {cfg.min_rr:g}, "
        f"surge {cfg.min_volume_surge:g}x, ...).")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=ROOT,
                    help="OptionsLab root holding the DBs (default: "
                         "$OPTIONSLAB_ROOT or /opt/optionslab)")
    ap.add_argument("--limit", type=int, default=2000,
                    help="max journal exits to replay (newest first)")
    ap.add_argument("--verbose", action="store_true",
                    help="print the per-trade verdict table")
    args = ap.parse_args(argv)

    tmp_reg = tmp_duck = None
    store = None
    try:
        tmp_reg = open_registry_copy(args.root)
        from app.core import registry
        from app.engines.scanner_trader import ScannerTrader
        exits = [r for r in registry.journal_rows(limit=args.limit,
                                                  kind="exit")
                 if r.get("entry_ts")]
        if not exits:
            print("No closed trades in scanner_journal — nothing to replay.")
            return 1
        cfg = ScannerTrader.__new__(ScannerTrader)._cfg()

        store, tmp_duck = open_store_copy(args.root)
        probe = store._q1("SELECT count(*) FROM stock_snapshots")
        if not probe or not probe[0]:
            print("stock_snapshots is empty in the market-data store — the "
                  "technical read cannot be reconstructed. Are you pointing "
                  "at the right --root?")
            return 1

        tech = reconstruct_tech(store, exits)
        verdicts = judge_trades(exits, tech, cfg)
        summary = summarize(verdicts, cfg)
        overlap = score_gate_overlap(verdicts, cfg)
        print_report(summary, verdicts, cfg, overlap=overlap,
                    verbose=args.verbose)
        return 0
    finally:
        try:
            if store is not None:
                store.con.close()
        except Exception:
            pass
        for tmp in (tmp_reg, tmp_duck):
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
