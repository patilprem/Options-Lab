# OptionsLab — what we are trying to achieve

This document is the *why*. `README.md` says what the app is,
`docs/ARCHITECTURE.md` says how the pieces fit, `docs/ROADMAP.md` says
what got built, `CLAUDE.md` says how to work on it. None of them say
what the thing is *for*. This does.

Read this first if you are picking the project up after a break, or if
you are about to make a decision the code can't make for you (should we
trade this? should we apply this parameter? is this ready for real
money?).

---

## 1. The goal, in one paragraph

Run a small book of Indian index/stock options **unattended**, on a VPS,
with a real and *measured* edge — not a hunch. The platform exists so
that every trading decision it makes is (a) generated from recorded
market data, (b) executed identically in backtest, paper and live, and
(c) improved only by changes that survived an out-of-sample test before
a human approved them. If the edge is not there, the platform's job is
to **prove that quickly and cheaply**, in paper, before any capital is
at risk.

Capital frame: **₹15 lac notional** — ₹10 lac allocated to Strategy
instances, ₹5 lac to the scanner auto-trader. Everything is currently
paper; live execution exists but is gated behind five separate switches
(see §7).

## 2. Where the edge is supposed to come from

Not from a clever formula. Three sources, in order of how much we
believe in them:

**(a) A data surface most retail traders don't have.**
Point-in-time recordings of the option chain — not just price, but IV,
OI, greeks, skew, PCR, max-pain — captured every few seconds and stored
with ATM-relative strike keys so they stay comparable across expiries
and spot levels. Plus underlying candles, index-futures volume (indexes
themselves carry zero volume), and MCX chain snapshots that Dhan does
**not** serve historically, so if we don't record them today they never
exist. This is the moat, and it compounds: every recorded session is a
session the backtester can replay and nobody can retro-fit.

**(b) Entry *quality*, not entry *direction*.**
The recurring chart-analysis exercise (`docs/chart_analysis.md`) keeps
landing on the same finding: the losses are rarely "wrong direction",
they're "right idea, bad price". So the system records, at every entry,
where the option premium sat relative to its own VWAP, Bollinger band,
and recent range — and the insight engines look for whether entering
closer to those references actually pays. That's a measurable question,
which is the point.

**(c) Not repeating our own documented mistakes.**
Churn, late entries, re-entering a name that just stopped us out,
giving back most of the MFE, fee drag on marginal setups. Each of these
is a rule in the insight engines with a numeric remedy attached, so a
pattern found in the trade log can become a parameter proposal instead
of a resolution to "trade better".

## 3. Why there are two engines

They answer different questions and neither replaces the other.

**Strategy instances** (`app/core/contract.py` → backtest / paper /
live) are *hypotheses*. You describe a strategy in English, an LLM
writes a class against a fixed `Context` API, and the exact same object
runs in the backtester and the paper engine. This is how we test a
specific idea over years of history — walk-forward, K-fold, in-sample
selection with out-of-sample reporting. Slow, rigorous, offline.

**The scanner auto-trader** (`app/engines/scanner_trader.py`) is
*discovery*. It sweeps ~210 F&O names every minute, shortlists on Tier-1
price/volume signals, deep-dives the chains of the survivors, and trades
what scores highest. It doesn't know what strategy it's running — it
learns which conditions pay by keeping a rich journal of every entry and
exit. It generates the trade population that the insight engines need in
order to have anything to say.

Together: the scanner finds *what* works across the market; a Strategy
turns a specific finding into a testable, backtestable rule. Losing
either one loses half the loop.

## 4. What "working" looks like — and what would kill it

Success is not "a green day". Explicitly:

- **Data**: recording is unbroken. `/data/health` shows no stale table
  across a full NSE session *and* a full MCX evening. Chain snapshots
  accumulate every trading day for every configured underlying. A
  five-day silent outage (which happened, see §6) is a failure of the
  *platform*, independent of P&L.
- **Statistical honesty**: enough closed round trips per bucket that
  insights clear their minimum-sample gates. Below that, the correct
  output is "not enough data", not advice.
- **Edge**: positive expectancy after real Indian charges
  (brokerage/STT/txn/GST/SEBI/stamp), sustained out-of-sample, with a
  drawdown a human is willing to sit through. Gross-of-fees profit is
  not evidence of anything.
- **Adaptation actually converging**: proposals that beat the champion
  in shadow keep beating it after being applied. If applied changes
  routinely get a "worse" verdict, the pipeline is measuring noise and
  we stop trusting it.

Kill criteria — write these down so they aren't rationalised away later:
if after a meaningful paper sample the expectancy is negative net of
fees, or the only profitable buckets are ones with too few trades to
trust, the answer is that this configuration has no edge. Change the
configuration or stop. The platform is built to make that verdict
*cheap to reach*, which is most of its value.

## 5. The discipline rules (why nothing auto-applies)

These are the non-negotiables. They exist because a system that tunes
itself on its own recent P&L will always find something, and it will
usually be noise.

1. **Insights propose; they never mutate.** The engines
   (`strategy_insights.py`, `journal_insights.py`) are pure functions
   over the trade log. The *only* code path that changes a setting is
   `apply_proposal()`, and it runs on a human click.
2. **Persistence before consideration.** A rule must fire on **≥3
   distinct days** before it can even arm a scan. One bad afternoon
   doesn't get to rewrite the config.
3. **Out-of-sample by construction.** Scanner changes are trialled on a
   **shadow book** — a challenger config trading virtually on the same
   live scores and quotes, ledger untouched. Strategy changes go through
   walk-forward: parameters are selected **in-sample** and *reported*
   out-of-sample, never selected on OOS.
4. **Beat the champion by a real margin, over ≥14 days**, on both the
   metric and realized P&L, before a proposal surfaces at all.
5. **One bounded step, inside hard clamps.** An apply moves a single
   parameter one step. No leaps.
6. **21-day embargo** after an apply, measured against the pre-change
   baseline. A "worse" verdict raises a revert warning. Dismissed rules
   cool down 30 days so they can't nag.
7. **Separate ledgers.** PAPER and LIVE P&L never sum, never share a
   row, never appear as one ₹ figure.
8. **One cost model.** Backtest and paper share `engines/fills.py`. The
   moment they fork, results stop being comparable and every conclusion
   above becomes unfalsifiable.

The human's role is deliberately narrow: **approve or reject** what the
shadow test surfaces. Not to tune knobs — to be the last gate on
changes the system has already justified with evidence.

## 6. Why the boring infrastructure work matters

It is tempting to treat recorder bugs as chores. They are not: **the
data is the product**. Two failures from the same week make the point.

- `_get_expiries` cached an *empty* expiry list for a whole day, poisoning
  the chain poller for the next session. Result: five days with zero
  chain snapshots for NIFTY/BANKNIFTY/CRUDEOIL/GOLD. That history cannot
  be re-fetched — for MCX, it cannot be reconstructed at all.
- The tick gate hardcoded NSE's 09:15–15:30 window for every underlying,
  so every MCX tick after 15:30 was silently discarded.

Both were silent. Neither showed up in P&L. This is why `/data/health`
is segment-aware, why the recorder distinguishes "no chain cache"
(outage) from "cached but unchanged" (frozen), and why a health check
that cries wolf is treated as a real bug — an alarm nobody trusts is
worse than no alarm.

## 7. Real money: what still stands in the way

Live execution (M8) is built and verified in dry-run — no real order has
ever been sent. Five gates must all be open before one can be:
`live_enabled` **and** `live_dry_run` off **and** static IP registered
**and** a per-strategy checklist acknowledged **and** the per-order
checks (market hours, lot cap, risk). `make_order_client()` returns a
`DryRunOrderClient` unless every one of them passes.

Known gaps that should be closed before real capital, regardless of how
good the paper numbers look:

- **Broker position reconciliation** — the app's view of what it holds
  is not yet checked against Dhan's.
- **Fill reconciliation** via the OrderUpdate WebSocket — we assume our
  fills; we don't confirm them.
- **Real FNO margin verification** during market hours (equity margin is
  live-verified; SPAN is not), plus `scripts/calibrate_margin`.
- **MCX chain recording** needs MCX security ids in
  `dhan_client.UNDERLYINGS`.

## 8. What this is deliberately not

- Not a product. Single user, single VPS, behind Tailscale. No auth, no
  multi-tenancy, no scale story.
- Not a sandbox. `loader.py` AST-scans pasted code and blocks the
  obvious (os/network/eval), but it is a guardrail against *mistakes*,
  not a defence against hostile code. Only paste code you wrote or
  generated.
- Not high-frequency. The cadences are seconds-to-minutes, bounded by
  Dhan's 1-request-per-3-seconds chain limit.
- Not a signal service or an advisory tool. Nothing here is a
  recommendation to anyone else.
- Not a promise of profit. Every number in the app is paper until §7 is
  closed, and the charges model in `fills.py` is an approximation until
  it's checked against a real contract note.

---

*If a change to this system can't be justified against §1–§5, it
probably shouldn't ship.*
