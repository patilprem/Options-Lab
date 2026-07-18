#!/usr/bin/env python3
"""Weekend strategy analysis — batch review of every strategy stored in the
registry, using the FULL recorded history in the store.

Markets are closed on weekends, so this is the natural window to re-evaluate
each stored strategy end-to-end. For every strategy (optionally filtered by
lifecycle state) it runs four analyses and prints a consolidated report:

  1. Backtest          — replay over the recorded coverage range: total P&L,
                         return %, max drawdown, Sharpe, trades, win-rate, fees.
  2. Walk-forward      — K-fold IS/OOS validation (engines/walkforward). Uses
                         the strategy's own `params` as a 1-point grid unless a
                         wider grid is given, so it measures out-of-sample
                         robustness, not an in-sample fit.
  3. Paper vs backtest — compares the strategy's live PAPER daily P&L (what it
                         actually did) against the backtest's average daily
                         expectation, and flags divergence.
  4. Monte Carlo       — order-shuffle of observed daily P&L (PAPER if present,
                         else backtest) to bound sequencing risk: p50/p90/p99
                         and worst max-drawdown.

READ-ONLY by default — it never mutates the registry or the store. Pass
`--save` to persist each backtest as a registry run (as the API's /backtest
endpoint does). Run ON THE VPS, where the real registry + market data live:

  venv/bin/python scripts/weekend_analysis.py                  # all strategies
  venv/bin/python scripts/weekend_analysis.py --states RUNNING,DEPLOYED_PAUSED
  venv/bin/python scripts/weekend_analysis.py --from 2025-01-01 --to 2025-06-30
  venv/bin/python scripts/weekend_analysis.py --lookback-days 120 --folds 4
  venv/bin/python scripts/weekend_analysis.py --out reports/weekend.md --json reports/weekend.json

In a dev container with no recorded data the store falls back to a synthetic
driver; the analysis still runs (on generated bars) so the logic can be
smoke-tested offline.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import loader, registry          # noqa: E402
from app.core.registry import State            # noqa: E402
from app.data.store import get_store           # noqa: E402
from app.engines import backtest as bt         # noqa: E402
from app.engines import walkforward as wf      # noqa: E402

DEFAULT_CAPITAL = 1_000_000.0


# --- helpers ----------------------------------------------------------------

def _instantiate(rec):
    """Build a strategy instance from stored code + persisted param overrides,
    mirroring app.api.strategies._instantiate (without importing the API layer,
    which would spin up the hub/runner singletons)."""
    obj = loader.load_strategy_class(rec.code)()
    overrides = registry.get_params(rec.id)
    if overrides and hasattr(obj, "params") and isinstance(obj.params, dict):
        obj.params.update(overrides)
    return obj


def _coverage_range(store, underlying: str):
    """(start, end) datetimes spanning the recorded bars for `underlying`,
    or (None, None) if unknown (synthetic store / no coverage)."""
    if not hasattr(store, "coverage"):
        return None, None
    try:
        rows, _opt = store.coverage()
    except Exception:
        return None, None
    for u, lo, hi, _n in rows:
        if u == underlying and lo and hi:
            lo = lo if isinstance(lo, datetime) else datetime.fromisoformat(str(lo))
            hi = hi if isinstance(hi, datetime) else datetime.fromisoformat(str(hi))
            return (lo.replace(hour=9, minute=15, second=0, microsecond=0),
                    hi.replace(hour=15, minute=30, second=0, microsecond=0))
    return None, None


def _resolve_range(store, underlying: str, args):
    """Pick the [start, end] the analyses run over: explicit --from/--to win,
    else the store's coverage, else a --lookback-days window ending today."""
    if args.from_date and args.to_date:
        return (datetime.fromisoformat(args.from_date + " 09:15:00"),
                datetime.fromisoformat(args.to_date + " 15:30:00"))
    lo, hi = _coverage_range(store, underlying)
    if lo and hi:
        if args.from_date:
            lo = max(lo, datetime.fromisoformat(args.from_date + " 09:15:00"))
        if args.to_date:
            hi = min(hi, datetime.fromisoformat(args.to_date + " 15:30:00"))
        return lo, hi
    # no coverage (e.g. synthetic) — fall back to a lookback window
    end = datetime.now().replace(hour=15, minute=30, second=0, microsecond=0)
    start = (end - timedelta(days=args.lookback_days)).replace(hour=9, minute=15)
    return start, end


def _montecarlo(pnls: list[float], n_sims: int) -> dict | None:
    """Order-shuffle MC on a daily-P&L series: same days, different order —
    bounds sequencing risk. Mirrors the /montecarlo API endpoint. None if the
    series is too short to be meaningful."""
    import random
    if len(pnls) < 5:
        return None
    max_dds = []
    for _ in range(min(n_sims, 5000)):
        seq = pnls[:]
        random.shuffle(seq)
        eq = peak = dd = 0.0
        for p in seq:
            eq += p
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        max_dds.append(dd)
    max_dds.sort()

    def pct(a, q):
        return round(a[min(len(a) - 1, int(q * len(a)))], 2)

    return {"sims": len(max_dds), "days": len(pnls),
            "total_pnl": round(sum(pnls), 2),
            "max_dd_p50": pct(max_dds, 0.5), "max_dd_p90": pct(max_dds, 0.9),
            "max_dd_p99": pct(max_dds, 0.99), "max_dd_worst": round(max_dds[-1], 2)}


def _paper_review(sid: str, bt_summary: dict, bt_daily: list) -> dict:
    """Compare live PAPER performance against the backtest expectation."""
    rows = registry.performance_rows(sid, "PAPER")
    paper_days = [{"date": r["trade_date"],
                   "pnl": round((r.get("realized") or 0.0) + (r.get("unrealized") or 0.0), 2)}
                  for r in rows]
    paper_total = round(sum(d["pnl"] for d in paper_days), 2)
    n_paper = len(paper_days)
    bt_days = len(bt_daily or [])
    bt_total = bt_summary.get("total_pnl", 0.0) if bt_summary else 0.0
    bt_avg = round(bt_total / bt_days, 2) if bt_days else None
    paper_avg = round(paper_total / n_paper, 2) if n_paper else None
    divergence = None
    if bt_avg is not None and paper_avg is not None:
        divergence = round(paper_avg - bt_avg, 2)
    return {"paper_days": n_paper, "paper_total": paper_total, "paper_avg": paper_avg,
            "backtest_avg": bt_avg, "avg_divergence": divergence,
            "_paper_pnls": [d["pnl"] for d in paper_days]}


# --- per-strategy analysis --------------------------------------------------

def analyse(rec, store, args) -> dict:
    out = {"id": rec.id, "name": rec.name,
           "state": rec.state.value if hasattr(rec.state, "value") else str(rec.state)}
    try:
        strat = _instantiate(rec)
        meta = strat.meta()
    except Exception as e:
        out["error"] = f"instantiate failed: {e!r}"
        return out
    underlying = meta.underlying
    capital = rec.allocated_capital or DEFAULT_CAPITAL
    start, end = _resolve_range(store, underlying, args)
    out["underlying"] = underlying
    out["range"] = [start.date().isoformat(), end.date().isoformat()]
    out["capital"] = capital

    if not store.has_data(underlying, start, end):
        out["error"] = f"no data for {underlying} in {out['range']}"
        return out

    # 1) backtest ------------------------------------------------------------
    bt_res = bt.run_backtest(_instantiate(rec), store, start, end, capital)
    if "error" in bt_res:
        out["error"] = f"backtest: {bt_res['error']}"
        return out
    out["backtest"] = bt_res["summary"]
    if args.save:
        run_id = f"weekend-{datetime.now():%Y%m%d%H%M%S}-{rec.id[:6]}"
        bt_res["status"] = "ok"
        registry.save_backtest(rec.id, run_id, out["range"][0], out["range"][1], bt_res)
        out["saved_run"] = run_id

    # 2) walk-forward --------------------------------------------------------
    def factory(params):
        obj = _instantiate(rec)
        if params and hasattr(obj, "params") and isinstance(obj.params, dict):
            obj.params.update(params)
        return obj

    try:
        wf_res = wf.run_walkforward(
            factory, store, underlying, start, end, folds=args.folds,
            is_frac=args.is_frac, param_grid=args.param_grid or {},
            capital=capital, metric=args.metric, max_runs=args.max_runs)
        out["walkforward"] = wf_res.get("aggregate_oos", {})
        out["walkforward"]["folds"] = len(wf_res.get("folds", []))
    except Exception as e:
        out["walkforward"] = {"error": repr(e)}

    # 3) paper vs backtest ---------------------------------------------------
    review = _paper_review(rec.id, bt_res["summary"], bt_res.get("daily", []))
    paper_pnls = review.pop("_paper_pnls")
    out["paper_review"] = review

    # 4) monte carlo ---------------------------------------------------------
    # prefer live PAPER days (real sequencing); fall back to backtest days.
    mc_source = "PAPER" if len(paper_pnls) >= 5 else "BACKTEST"
    mc_pnls = paper_pnls if mc_source == "PAPER" else [
        round((d.get("realized") or 0.0) + (d.get("unrealized_eod") or 0.0), 2)
        for d in bt_res.get("daily", [])]
    mc = _montecarlo(mc_pnls, args.mc_sims)
    if mc:
        mc["source"] = mc_source
    out["montecarlo"] = mc
    return out


# --- reporting --------------------------------------------------------------

def _fmt_inr(x):
    try:
        return f"₹{round(float(x)):,}"
    except (TypeError, ValueError):
        return "—"


def render_markdown(results: list[dict], generated: str) -> str:
    L = [f"# Weekend strategy analysis\n", f"_Generated {generated} · {len(results)} strategies_\n"]
    for r in results:
        L.append(f"\n## {r['name']}  `{r.get('state','?')}`")
        if r.get("error"):
            L.append(f"\n> ⚠️ {r['error']}\n")
            continue
        L.append(f"\n*{r['underlying']} · {r['range'][0]} → {r['range'][1]} · "
                 f"capital {_fmt_inr(r['capital'])}*\n")
        b = r.get("backtest", {})
        L.append("**Backtest** — "
                 f"P&L {_fmt_inr(b.get('total_pnl'))} ({b.get('return_pct','—')}%) · "
                 f"maxDD {b.get('max_drawdown_pct','—')}% · Sharpe {b.get('sharpe','—')} · "
                 f"{b.get('n_trades','—')} trades · win {b.get('win_rate_pct','—')}% · "
                 f"fees {_fmt_inr(b.get('total_fees'))}")
        w = r.get("walkforward", {})
        if "error" in w:
            L.append(f"\n**Walk-forward** — error: {w['error']}")
        else:
            L.append(f"\n**Walk-forward** ({w.get('folds','?')} folds, OOS) — "
                     f"return {w.get('return_pct','—')}% · "
                     f"maxDD {w.get('max_drawdown_pct','—')}% · {w.get('days','—')} OOS days")
        pr = r.get("paper_review", {})
        if pr.get("paper_days"):
            L.append(f"\n**Paper vs backtest** — {pr['paper_days']} paper days, "
                     f"total {_fmt_inr(pr['paper_total'])}, avg/day {_fmt_inr(pr['paper_avg'])} "
                     f"vs backtest avg/day {_fmt_inr(pr['backtest_avg'])} "
                     f"(divergence {_fmt_inr(pr['avg_divergence'])})")
        else:
            L.append("\n**Paper vs backtest** — no live paper days yet")
        mc = r.get("montecarlo")
        if mc:
            L.append(f"\n**Monte Carlo** ({mc['source']}, {mc['sims']} sims / {mc['days']} days) — "
                     f"max-DD p50 {_fmt_inr(mc['max_dd_p50'])} · p90 {_fmt_inr(mc['max_dd_p90'])} · "
                     f"p99 {_fmt_inr(mc['max_dd_p99'])} · worst {_fmt_inr(mc['max_dd_worst'])}")
        else:
            L.append("\n**Monte Carlo** — need ≥5 days of P&L")
        L.append("")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Weekend batch analysis of stored strategies")
    ap.add_argument("--states", default="",
                    help="comma list to filter (e.g. RUNNING,DEPLOYED_PAUSED); default all")
    ap.add_argument("--from", dest="from_date", default="", help="YYYY-MM-DD range start")
    ap.add_argument("--to", dest="to_date", default="", help="YYYY-MM-DD range end")
    ap.add_argument("--lookback-days", type=int, default=180,
                    help="window when the store has no coverage info (default 180)")
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--is-frac", type=float, default=0.7)
    ap.add_argument("--metric", default="sharpe", help="walk-forward IS metric")
    ap.add_argument("--max-runs", type=int, default=200)
    ap.add_argument("--param-grid", default="",
                    help='JSON grid for walk-forward, e.g. \'{"sl_pct":[0.2,0.3,0.4]}\'')
    ap.add_argument("--mc-sims", type=int, default=1000)
    ap.add_argument("--save", action="store_true",
                    help="persist each backtest as a registry run (default: read-only)")
    ap.add_argument("--out", default="", help="write the markdown report here")
    ap.add_argument("--json", dest="json_out", default="", help="write raw JSON results here")
    args = ap.parse_args(argv)
    args.param_grid = json.loads(args.param_grid) if args.param_grid else {}

    registry.init_db()
    store = get_store()
    store_kind = type(store).__name__
    if store_kind != "DataStore":
        print(f"WARNING: market store is {store_kind} (no real recorded data). "
              "Results are on synthetic bars — run this on the VPS for real numbers.",
              file=sys.stderr)

    wanted = {s.strip().upper() for s in args.states.split(",") if s.strip()}
    strategies = registry.list_all()
    if wanted:
        strategies = [s for s in strategies
                      if (s.state.value if hasattr(s.state, "value") else str(s.state)) in wanted]

    if not strategies:
        print("No strategies found in the registry"
              + (f" for states {sorted(wanted)}" if wanted else "")
              + ". Nothing to analyse.", file=sys.stderr)
        return 1

    print(f"Analysing {len(strategies)} strateg{'y' if len(strategies)==1 else 'ies'} "
          f"(store={store_kind})…", file=sys.stderr)
    results = []
    for rec in strategies:
        print(f"  · {rec.name} ({rec.id[:8]})…", file=sys.stderr)
        results.append(analyse(rec, store, args))

    generated = datetime.now().isoformat(sep=" ", timespec="seconds")
    md = render_markdown(results, generated)
    print(md)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"\n[report written to {args.out}]", file=sys.stderr)
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
        print(f"[json written to {args.json_out}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
