#!/usr/bin/env python3
"""Volatility-premium test — is there premium to HARVEST in index options?

The one question left after every intraday strategy failed out of sample
(docs/entry_exit_findings.md): do NIFTY/BANKNIFTY options, on average, price
MORE movement than actually follows? If yes, a positional, defined-risk
premium seller (weekly iron condor) has something to harvest. If no, there is
no premium to harvest and the platform has no remaining hypothesis worth
paying data for.

Runs on the ALREADY-DOWNLOADED backfill (option_bars WEEKLY 0, offsets -5..5,
IV on every row + underlying_bars). Needs no Dhan subscription and no network.

Method — model-free first, model-dependent second:
  1. Expiries: expiry_calendar if present, else DERIVED from the data
     (app/data/expiries.detect_weekly_expiries — the backfill stores
     expiry=NULL). A cycle whose length is far out of line with its
     neighbours means an expiry was missed; its trades are dropped, never
     priced against the wrong contract.
  2. For each expiry E, enter `dte` trading days before it at --entry-time
     (one trade per expiry, so trades never overlap and the t-stat is honest).
  3. Hold to expiry and settle at intrinsic off the underlying's last close
     on E. Two structures:
       STRADDLE  sell ATM CE+PE        — the purest read of the premium
       CONDOR    sell CE +s / PE -s, buy CE +(s+w) / PE -(s+w)
                 — the strategy actually proposed (risk capped by the wings)
     Entry fills and fees go through engines/fills.fill_backtest (the SAME
     cost model backtest and paper use — invariant #2). Legs expiring
     worthless are left to expire (no exit order, no fee); legs with
     intrinsic value are closed at intrinsic through the same fill model.
  4. Secondary: ATM IV at entry vs realized vol entry->expiry (log returns of
     every recorded bar, overnight gaps included, annualized on calendar
     days). Dhan's IV day-count convention is undocumented, so treat the
     IV/RV ratio as indicative; the P&L in (3) is the real test.

PRE-REGISTERED VERDICT (decided before seeing results; only the PRIMARY dte
counts — other dtes are printed as exploratory and must NOT be cherry-picked):
  PREMIUM PRESENT  straddle GROSS mean > 0 with t >= 2
  TRADEABLE        condor NET mean > 0 with t >= 2, AND net-positive in both
                   the first and second half of the sample, AND no single
                   calendar month exceeds the whole sample's net profit
  Anything else -> NO EDGE: stop, cancel the data subscription.
Even a pass is in-sample: it earns a forward PAPER trial, not capital.

READ-ONLY. DuckDB's lock is exclusive while the service runs, so by default
this queries a temp COPY of marketdata.duckdb (needs free disk ~= DB size).
Pass --db to read a file directly when the service is stopped.

Usage (on the VPS):
    venv/bin/python -m scripts.vol_premium_test
    venv/bin/python -m scripts.vol_premium_test --underlying NIFTY \\
        --dte 3 --explore-dte 1,2,4 --short 2 --wing 2 --entry-time 10:00
"""
from __future__ import annotations

import argparse
import glob
import math
import os
import shutil
import statistics
import sys
import tempfile
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.contract import Action                              # noqa: E402
from app.data.expiries import detect_weekly_expiries               # noqa: E402
from app.engines.backtest import lot_size_on                       # noqa: E402
from app.engines.fills import FeeConfig, SlippageConfig, fill_backtest  # noqa: E402

ROOT = os.environ.get("OPTIONSLAB_ROOT", "/opt/optionslab")
SESSION_CLOSE = time(15, 30)


# ---------------------------------------------------------------------------
# data access (every function takes a `store` with ._q(sql, params))
# ---------------------------------------------------------------------------

def load_expiries(store, underlying: str) -> list[date]:
    """Persisted calendar when present, else derived from the premium collapse."""
    try:
        rows = store._q("SELECT expiry FROM expiry_calendar WHERE underlying=? "
                        "ORDER BY expiry", [underlying])
        if rows:
            return [r[0] for r in rows]
    except Exception:
        pass                                   # table absent: derive instead
    return detect_weekly_expiries(store, underlying)


def suspect_cycles(expiries: list[date], ratio: float = 1.5) -> set[date]:
    """Expiries whose cycle (gap since the previous expiry) is > `ratio` x the
    median of the surrounding gaps — the signature of a MISSED expiry in
    between, where the offset-0 contract held on the entry day is not E.
    Median of neighbours, not a fixed 7 days, so a weekly->monthly cadence
    change (BANKNIFTY, Nov-2024) is not mistaken for a gap."""
    gaps = [(expiries[i], (expiries[i] - expiries[i - 1]).days)
            for i in range(1, len(expiries))]
    bad = set()
    for i, (e, g) in enumerate(gaps):
        around = [x for _, x in gaps[max(0, i - 3):i] + gaps[i + 1:i + 4]]
        if around and g > ratio * statistics.median(around):
            bad.add(e)
    return bad


def trading_days(store, underlying: str) -> list[date]:
    return [r[0] for r in store._q(
        "SELECT DISTINCT CAST(ts AS DATE) d FROM underlying_bars "
        "WHERE underlying=? ORDER BY d", [underlying])]


def leg_at(store, underlying: str, day: date, at: datetime,
           strike_offset: int, option_type: str):
    """(close, strike, iv, expiry) of the WEEKLY-0 leg at the last bar <= at."""
    return store._q1(
        """SELECT close, strike, iv, expiry FROM option_bars
           WHERE underlying=? AND expiry_kind='WEEKLY' AND expiry_offset=0
             AND strike_offset=? AND option_type=?
             AND ts >= ? AND ts <= ?
           ORDER BY ts DESC LIMIT 1""",
        [underlying, strike_offset, option_type,
         datetime.combine(day, time(9, 15)), at])


def spot_at(store, underlying: str, day: date, at: datetime):
    r = store._q1(
        """SELECT close FROM underlying_bars WHERE underlying=?
             AND ts >= ? AND ts <= ? ORDER BY ts DESC LIMIT 1""",
        [underlying, datetime.combine(day, time(9, 15)), at])
    return r[0] if r else None


def realized_vol(store, underlying: str, start: datetime, end: datetime):
    """Annualized realized vol (%) from every recorded bar in (start, end],
    overnight gaps included (an option prices them too). Calendar-day
    annualization; None when there are too few bars to mean anything."""
    closes = [r[0] for r in store._q(
        """SELECT close FROM underlying_bars WHERE underlying=?
             AND ts >= ? AND ts <= ? AND close > 0 ORDER BY ts""",
        [underlying, start, end])]
    if len(closes) < 10:
        return None
    ss = sum(math.log(b / a) ** 2 for a, b in zip(closes, closes[1:]))
    tau = (end - start).total_seconds() / (365 * 86400)
    return math.sqrt(ss / tau) * 100 if tau > 0 else None


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    underlying: str
    entry_day: date
    expiry: date
    structure: str
    lot: int
    spot_entry: float
    spot_expiry: float
    credit_pts: float          # net premium collected per unit (mid)
    gross: float               # Rs, at mid/intrinsic, no costs
    net: float                 # Rs, after spread + fees
    costs: float
    max_loss: float | None = None   # condor only: wing width x lot - credit
    atm_iv: float | None = None
    rv: float | None = None


def intrinsic(option_type: str, strike: float, spot: float) -> float:
    return max(spot - strike, 0.0) if option_type == "CALL" else max(strike - spot, 0.0)


def price_structure(legs: list[tuple[int, str, Action, float, float]],
                    spot_expiry: float, qty: int,
                    fee: FeeConfig, slip: SlippageConfig) -> tuple[float, float, float]:
    """legs: (strike_offset, option_type, entry action, entry close, strike).
    Hold to expiry. -> (gross Rs, net Rs, credit pts per unit at mid)."""
    gross = net = credit = 0.0
    for off, ot, act, px, k in legs:
        sign = 1 if act == Action.SELL else -1
        settle = intrinsic(ot, k, spot_expiry)
        credit += sign * px
        gross += sign * (px - settle) * qty
        f_in = fill_backtest(px, off, act, qty, fee, slip)
        cost = f_in.fees
        exit_px = 0.0
        if settle > 0:                         # worthless legs just expire
            back = Action.BUY if act == Action.SELL else Action.SELL
            f_out = fill_backtest(settle, off, back, qty, fee, slip)
            exit_px, cost = f_out.price, cost + f_out.fees
        net += sign * (f_in.price - exit_px) * qty - cost
    return gross, net, credit


def build_trades(store, underlying: str, dte: int, entry_time: time,
                 short_off: int, wing: int,
                 fee: FeeConfig | None = None, slip: SlippageConfig | None = None,
                 skipped: dict | None = None) -> list[Trade]:
    """One straddle + one condor per expiry, entered `dte` trading days before."""
    fee, slip = fee or FeeConfig(), slip or SlippageConfig()
    skipped = skipped if skipped is not None else defaultdict(int)
    expiries = load_expiries(store, underlying)
    bad = suspect_cycles(expiries)
    days = trading_days(store, underlying)
    out: list[Trade] = []
    for i, e in enumerate(expiries):
        if e in bad:
            skipped["suspect cycle (missed expiry?)"] += 1
            continue
        j = bisect_left(days, e)
        if j >= len(days) or days[j] != e or j - dte < 0:
            skipped["expiry/entry day not in underlying_bars"] += 1
            continue
        d = days[j - dte]
        if i > 0 and d <= expiries[i - 1]:
            skipped["entry before previous expiry (cycle too short)"] += 1
            continue
        at = datetime.combine(d, entry_time)
        spot0 = spot_at(store, underlying, d, at)
        spot_e = spot_at(store, underlying, e, datetime.combine(e, SESSION_CLOSE))
        if not spot0 or not spot_e:
            skipped["no spot at entry/expiry"] += 1
            continue
        lot = lot_size_on(underlying, d)

        def legs_for(spec):
            legs = []
            for off, ot, act in spec:
                r = leg_at(store, underlying, d, at, off, ot)
                if not r or not r[0] or r[0] <= 0 or not r[1]:
                    return None, f"missing leg {ot} {off:+d}"
                if r[3] is not None and r[3] != e:
                    return None, "row expiry disagrees with calendar"
                legs.append((off, ot, act, float(r[0]), float(r[1])))
            return legs, None

        straddle, why = legs_for([(0, "CALL", Action.SELL), (0, "PUT", Action.SELL)])
        if straddle is None:
            skipped[why] += 1
            continue
        ivs = [x[2] for x in (leg_at(store, underlying, d, at, 0, "CALL"),
                              leg_at(store, underlying, d, at, 0, "PUT"))
               if x and x[2] and x[2] > 0]
        atm_iv = sum(ivs) / len(ivs) if ivs else None
        rv = realized_vol(store, underlying, at, datetime.combine(e, SESSION_CLOSE))
        g, n, c = price_structure(straddle, spot_e, lot, fee, slip)
        out.append(Trade(underlying, d, e, "STRADDLE", lot, spot0, spot_e, c,
                         g, n, g - n, None, atm_iv, rv))

        s, w = short_off, short_off + wing
        condor, why = legs_for([(s, "CALL", Action.SELL), (-s, "PUT", Action.SELL),
                                (w, "CALL", Action.BUY), (-w, "PUT", Action.BUY)])
        if condor is None:
            skipped[f"condor: {why}"] += 1
            continue
        k = {(o, t): kk for o, t, _, _, kk in condor}
        if not (k[(w, "CALL")] > k[(s, "CALL")] > k[(-s, "PUT")] > k[(-w, "PUT")]):
            skipped["condor: strikes out of order"] += 1
            continue
        g, n, c = price_structure(condor, spot_e, lot, fee, slip)
        width = max(k[(w, "CALL")] - k[(s, "CALL")], k[(-s, "PUT")] - k[(-w, "PUT")])
        out.append(Trade(underlying, d, e, "CONDOR", lot, spot0, spot_e, c,
                         g, n, g - n, (width - c) * lot, atm_iv, rv))
    return out


# ---------------------------------------------------------------------------
# statistics + verdict
# ---------------------------------------------------------------------------

def tstat(xs: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    sd = statistics.stdev(xs)
    return statistics.mean(xs) / (sd / math.sqrt(len(xs))) if sd > 0 else None


def summarize(trades: list[Trade], key: str = "net") -> dict:
    xs = [getattr(t, key) for t in trades]
    if not xs:
        return {"n": 0}
    half = len(trades) // 2
    by_month = defaultdict(float)
    for t in trades:
        by_month[t.expiry.strftime("%Y-%m")] += getattr(t, key)
    wins = [x for x in xs if x > 0]
    losses = [x for x in xs if x < 0]
    return {
        "n": len(xs), "mean": statistics.mean(xs), "median": statistics.median(xs),
        "t": tstat(xs), "total": sum(xs), "win": len(wins) / len(xs),
        "worst": min(xs), "best": max(xs),
        "pf": (sum(wins) / -sum(losses)) if losses else None,
        "first_half": sum(xs[:half]), "second_half": sum(xs[half:]),
        "worst_month": min(by_month.items(), key=lambda kv: kv[1]),
        "best_month": max(by_month.items(), key=lambda kv: kv[1]),
        "costs_mean": statistics.mean(t.costs for t in trades),
    }


def verdict(straddle: list[Trade], condor: list[Trade]) -> tuple[str, list[str]]:
    """The pre-registered rule from the module docstring. -> (label, reasons)."""
    why = []
    sg = summarize(straddle, "gross")
    premium = sg["n"] >= 20 and sg["mean"] > 0 and (sg["t"] or 0) >= 2
    why.append(f"straddle gross mean {_rs(sg.get('mean'))}, t={_f(sg.get('t'))}, "
               f"n={sg['n']} -> premium {'PRESENT' if premium else 'NOT shown'}")
    cn = summarize(condor, "net")
    if cn["n"] == 0:
        return ("PREMIUM PRESENT (condor untestable)" if premium else "NO EDGE"), why
    checks = {
        "condor net mean > 0": cn["mean"] > 0,
        "condor net t >= 2": (cn["t"] or 0) >= 2,
        "net positive in BOTH halves": cn["first_half"] > 0 and cn["second_half"] > 0,
        "no single month carries the total": cn["best_month"][1] < cn["total"]
        if cn["total"] > 0 else False,
        "enough trades (n >= 20)": cn["n"] >= 20,
    }
    for k, ok in checks.items():
        why.append(f"{'PASS' if ok else 'FAIL'}  {k}")
    if all(checks.values()):
        return "TRADEABLE (in-sample) -> forward PAPER trial, not capital", why
    if premium:
        return "PREMIUM PRESENT, NOT TRADEABLE after costs/structure", why
    return "NO EDGE", why


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _rs(x):
    return "—" if x is None else f"Rs.{x:,.0f}"


def _f(x, nd=2):
    return "—" if x is None else f"{x:.{nd}f}"


def _block(title: str, trades: list[Trade]) -> list[str]:
    g, n = summarize(trades, "gross"), summarize(trades, "net")
    if n["n"] == 0:
        return [f"  {title}: no trades"]
    lines = [f"  {title}  (n={n['n']}, {trades[0].entry_day} .. {trades[-1].expiry})",
             f"    gross/trade  mean {_rs(g['mean'])}  t={_f(g['t'])}",
             f"    net/trade    mean {_rs(n['mean'])}  median {_rs(n['median'])}  "
             f"t={_f(n['t'])}  win {n['win']:.0%}  PF {_f(n['pf'])}",
             f"    costs/trade  {_rs(n['costs_mean'])}   total net {_rs(n['total'])}",
             f"    halves       1st {_rs(n['first_half'])}   2nd {_rs(n['second_half'])}",
             f"    worst trade  {_rs(n['worst'])}   worst month {n['worst_month'][0]} "
             f"{_rs(n['worst_month'][1])}   best month {n['best_month'][0]} "
             f"{_rs(n['best_month'][1])}"]
    ml = [t.max_loss for t in trades if t.max_loss]
    if ml:
        lines.append(f"    max loss/lot (wings) mean {_rs(statistics.mean(ml))}  -> "
                     f"net/trade = {n['mean'] / statistics.mean(ml):.1%} of risk")
    by_year = defaultdict(list)
    for t in trades:
        by_year[t.expiry.year].append(t.net)
    lines.append("    by year      " + "   ".join(
        f"{y}: {_rs(sum(v))} ({len(v)})" for y, v in sorted(by_year.items())))
    return lines


def _vol_block(trades: list[Trade]) -> list[str]:
    pairs = [(t.atm_iv, t.rv) for t in trades if t.atm_iv and t.rv]
    if not pairs:
        return ["  IV vs RV: no IV recorded"]
    iv, rv = zip(*pairs)
    diff = [a - b for a, b in pairs]
    return [f"  IV vs RV (indicative; Dhan IV day-count undocumented), n={len(pairs)}",
            f"    mean ATM IV {statistics.mean(iv):.1f}%   mean realized "
            f"{statistics.mean(rv):.1f}%   IV > RV in {sum(d > 0 for d in diff) / len(diff):.0%}"
            f"   mean IV-RV {statistics.mean(diff):+.1f} pts (t={_f(tstat(diff))})"]


def report(store, underlying: str, dte: int, explore: list[int], entry_time: time,
           short_off: int, wing: int) -> str:
    out = [f"\n=== {underlying}: volatility-premium test ===",
           f"entry {entry_time:%H:%M}, {dte} trading days before expiry, hold to "
           f"expiry; condor short +/-{short_off} wing {wing} strikes"]
    skipped: dict = defaultdict(int)
    trades = build_trades(store, underlying, dte, entry_time, short_off, wing,
                          skipped=skipped)
    st = [t for t in trades if t.structure == "STRADDLE"]
    co = [t for t in trades if t.structure == "CONDOR"]
    if skipped:
        out.append("skipped: " + ", ".join(f"{k} x{v}" for k, v in skipped.items()))
    out.append(f"\nPRIMARY (dte={dte}) — the only one the verdict uses")
    out += _vol_block(st)
    out += _block("STRADDLE (sell ATM CE+PE)", st)
    out += _block("CONDOR", co)
    label, why = verdict(st, co)
    out.append(f"\n  VERDICT: {label}")
    out += [f"    {w}" for w in why]
    for x in explore:
        if x == dte:
            continue
        ts = build_trades(store, underlying, x, entry_time, short_off, wing)
        out.append(f"\nEXPLORATORY dte={x} (do NOT pick the best of these — "
                   "that is curve-fitting)")
        out += _block("STRADDLE", [t for t in ts if t.structure == "STRADDLE"])
        out += _block("CONDOR", [t for t in ts if t.structure == "CONDOR"])
    return "\n".join(out)


def open_store(db: str | None):
    """-> (store, tempdir or None). Never get_store(): under the service's
    lock it silently returns SyntheticStore, and a test on synthetic prices
    would print a confident, meaningless verdict."""
    from app.data.store import DataStore
    if db:
        return DataStore(Path(db)), None
    src = os.path.join(ROOT, "marketdata.duckdb")
    if not os.path.exists(src):
        raise FileNotFoundError(f"no market-data store at {src} "
                                "(set OPTIONSLAB_ROOT or pass --db)")
    tmp = tempfile.mkdtemp(prefix="olab-volprem-")
    for f in glob.glob(src + "*"):
        shutil.copy(f, tmp)
    return DataStore(Path(tmp) / "marketdata.duckdb"), tmp


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--underlying", nargs="+", default=["NIFTY", "BANKNIFTY"])
    ap.add_argument("--dte", type=int, default=3,
                    help="PRIMARY entry: trading days before expiry (default 3)")
    ap.add_argument("--explore-dte", default="1,2,4",
                    help="extra dtes printed as exploratory only")
    ap.add_argument("--entry-time", type=time.fromisoformat, default=time(10, 0))
    ap.add_argument("--short", type=int, default=2, help="condor short strikes OTM")
    ap.add_argument("--wing", type=int, default=2, help="wing width in strikes")
    ap.add_argument("--db", help="read this DuckDB file directly (service stopped)")
    args = ap.parse_args()
    if args.short + args.wing > 5:
        ap.error("backfill only covers offsets -5..+5: short + wing must be <= 5")
    explore = [int(x) for x in args.explore_dte.split(",") if x.strip()]

    store, tmp = open_store(args.db)
    try:
        for u in args.underlying:
            print(report(store, u, args.dte, explore, args.entry_time,
                         args.short, args.wing), flush=True)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
