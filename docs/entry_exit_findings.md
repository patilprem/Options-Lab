# Where the scanner-trader's money goes (2026-07-22 .. 07-31)

Findings from measuring the live book rather than reading charts. Every number
here is reproducible from recorded data with the scripts named below. **Nothing
in here has been applied to live settings** — the only settings-mutation path
remains `apply_proposal()` behind a human click (see `adaptation.py`).

Sample: **77 closed round trips over 8 trading days**, one market regime. Small.
Treat everything as a hypothesis with a measured effect size, not a law.

## The headline

**Net ‑₹71,849 over the window, 14/77 wins (18%).** The signal is not the
problem — the premiums it picked ran **+35% to +199%** on the days reviewed. The
money is lost in the mechanics on either side of the pick:

| | finding | measured |
|---|---|---|
| **Exit** | a ~30% trail on trades that pop +20–40% and revert | **₹79,653** recoverable |
| **Entry** | signals fire *after* the move is over | median MFE **+0.0%** |

## Exit: the trail cannot pay on these trades

`scripts/exit_counterfactual.py` — every closed trade records its MFE (the best
it ever traded), so the counterfactual needs no path simulation: MFE proves the
price reached that level.

| Policy | Net P&L | Δ vs actual | Win rate |
|---|---|---|---|
| Actual | ‑₹71,849 | — | 18% |
| TP +20% | +₹5,179 | +₹77,028 | 45% |
| **TP +25%** | **+₹7,804** | **+₹79,653** | 41% |
| TP +30% | +₹5,617 | +₹77,466 | 37% |

**The mechanism.** A trailing stop of fraction *t* only pays if the peak clears
*t/(1‑t)* — **~43% at t=0.30**. The book's trades peak at +20–40% and revert, so
the trail sits at or below the entry price and converts a favorable excursion
into a red exit *by construction*. Confirmed on 07-31: five trail exits gave
back MFEs of 21–41% while the setup score was still 92–97.

`target_pct` (a take-profit) already exists and is enforced every bar — it is
set to **1.00 (+100%)**, which never fires. The take-profit isn't missing, it's
switched off.

**Status:** wired for a shadow trial. New `mfe_take_profit` insight rule
(`journal_insights.py`) → `target_pct` 1.00 → 0.25 (`adaptation.py`). Kept as a
*second* rule alongside `trail_giveback` deliberately: both are valid remedies
for the same evidence, and the virtual book decides which wins rather than us.

**Caveat that matters:** even at the optimum this is **+₹7,804 over 8 days** —
roughly break-even, not profitable. The exit fix stops the bleeding; it does not
create edge. The optimum is a plateau (20–30% within ₹2.6k), which is reassuring
against curve-fitting, but +25% was still chosen on the same data it scores on.

## Entry: the signals fire too late

`scripts/entry_retrace_study.py` — replays each signal against that option's own
recorded `chain_snapshots` premium+volume series.

**The damning baseline: median MFE from the actual entry is +0.0%, and the
median 60-minute run is ‑5.3%.** Half of all trades never trade above the buy
price at any point in the following hour. Every entry across four sessions
carries `pressing the day's high/low` with `range_pos` **0.92–0.99** and a
3–5× volume surge — the bot only buys at the extreme of the day's range.

### Does waiting for a pullback help? Yes — and the placebo proves the level matters

| Policy | Fill rate | Edge vs chase | Patient better on |
|---|---|---|---|
| ‑15% pullback | 22% | **+12.3%** | **17/17 (100%)** |
| ‑10% pullback | 29% | **+11.7%** | 22/23 (96%) |
| ‑5% pullback | 50% | +7.0% | 37/39 (95%) |
| MA20 touch | 67% | +6.1% | 28/29 (97%) |
| VWAP touch | 58% | +4.3% | 23/24 (96%) |
| *PLACEBO wait 10m* | 92% | +3.1% | *48/71 (68%)* |
| *PLACEBO wait 20m* | 77% | **‑0.3%** | *33/60 (55%)* |

The placebo arms buy after a fixed delay with **no price condition**. They are
near-worthless (‑0.3% edge, 55% hit rate — a coin flip), while the level-based
policies win 96–100% of the time at 2–4× the edge. So the *reference level* is
doing real work; the delay is not.

### Three reasons this is not yet a shippable rule

1. **The absolute edge is thin.** Buying 18.7% cheaper then running +6.9% =
   **87% of the original signal price** — it never recovers to where the bot
   bought. +6.9% is MFE, not realized, and fees are ~₹40/round trip on ₹13–80
   premiums. It is also far below the +25% the take-profit needs, so the two
   fixes would rarely co-fire.
2. **A 6-minute blind spot.** Median chain poll gap after a signal is **6 min**
   and median pullback wait is **6–7 min** — most "pullbacks" are detected at the
   first observable price. The move happened inside a window the data cannot
   see, so a live limit order's fill price is genuinely unknown.
3. **Residual selection bias.** The placebo controls for *delay*, not for
   *conditioning on a dip*: buying a local low mechanically inflates forward
   MFE. Part of +12% is real, part is arithmetic. Separating them needs realized
   P&L under a fixed exit, not MFE.

## What this points at

Both fixes are tuning around a signal whose median outcome is **+0.0% MFE**.
`entry_score` is built from `price_change_pct`, `volume_surge` and OI change —
all backward-looking, all describing a move that already happened, which is why
entries land at `range_pos` 0.92–0.99. Improving *when* we act on a late signal
is worth a few percent. Making the signal fire earlier is worth the rest.

The index-breadth precursor question (`scripts/move_precursors.py`) is the
natural place to look for an earlier trigger, but it **cannot be answered yet**:
`index_bias_history` only starts 2026-07-17, so of 1,238 detected NIFTY moves
only ~13 had breadth recorded. Re-run after ~30 more trading days.

## Index findings (2024-07 .. 2026-07, ~2,900 moves, both indices)

Run on `option_bars` — the expired-options backfill, 508/507 days per index with
IV on every row — so coverage is **100% of detected moves**, not the 1% the live
chain recording allowed. A "move" is a forward return over 6 bars (30 min)
clearing 0.25%, collapsed to the leg's launch bar. Scored by AUC (Mann-Whitney):
0.50 = cannot tell ups from downs; z = standard errors from chance.

### 1. The indices MEAN-REVERT at 30 minutes — and it explains the losses

`[CTRL] price drift %` over the 30 min before a move: **AUC 0.38 (NIFTY, z ‑7.0)
/ 0.39 (BANKNIFTY, z ‑6.9)**. Below 0.50 means price drifts DOWN before up-moves
and UP before down-moves. Replicated on two independent indices at ~7 sigma.

This is the same fact the entry study found from the other side: chase entries
at `range_pos` 0.92–0.99 have median MFE **+0.0%** and a median 60-min run of
**‑5.3%**. The scanner buys extremes; at this horizon extremes revert. Two
independent datasets, one conclusion — **the entry logic is positioned against
the dominant short-horizon behaviour of the market it trades.**

### 2. Option-writer OI flow carries real, independent information

`Δ PCR (OI)` over the same 30 min: **AUC 0.64 (NIFTY, z +7.9) / 0.62
(BANKNIFTY, z +8.0)**, and the direction is economically coherent —

| | before UP | before DOWN |
|---|---|---|
| Δ PCR (OI) | +0.027 | ‑0.026 |
| Δ put OI | 1,988,155 | 695,725 |
| Δ call OI | 920,775 | 2,495,400 |

Put writers add OI before rallies; call writers add before declines. Selling a
put is a bullish position and selling a call a bearish one, so this reads as
option writers positioning ahead of the move.

**Confound tested and rejected.** OI flow could simply RESPOND to a move already
under way. Two checks:
- the momentum story is refuted outright — price drift discriminates the WRONG
  way (0.38, not >0.50);
- the proxy story (price falls → puts written → PCR rises, and the fall predicts
  the bounce) is tested by re-scoring inside price-drift terciles:

| | overall | drift LOW | drift MID | drift HIGH |
|---|---|---|---|---|
| NIFTY | 0.64 | 0.64 (+4.5) | 0.61 (+3.4) | 0.63 (+4.2) |
| BANKNIFTY | 0.62 | 0.55 (+1.8) | 0.61 (+4.3) | 0.57 (+2.5) |

NIFTY shows **no attenuation** — the signal is independent of price drift.
BANKNIFTY attenuates (outer buckets fall below the z>=3 bar) but retains a
significant core, so part of its raw effect was drift-related and part was not.

### 3. What did NOT hold

- **IV skew**: 0.55 / 0.53 (z +2.9 / +2.1) — below the bar on both.
  `scanner.py`'s comment calls positive skew "downside fear... the classic
  pre-fall tell". On 2,900 moves that claim is not supported.
- **ATM IV**: 0.54 / 0.50 — nothing.
- **Δ IV skew**: 0.51 / 0.47 — nothing.
- **max-pain distance**: 0.42 / 0.46 — weak, and it does not replicate cleanly.

### Before any of this is traded

1. **It is IN-SAMPLE.** Every one of these two years was used for discovery. A
   train/test split now is pseudo-OOS at best — the honest out-of-sample test is
   forward, via the shadow-trial machinery.
2. **AUC 0.62–0.64 is a WEAK discriminator.** It ranks a random up-move above a
   random down-move ~63% of the time versus 50% by chance. Statistically
   overwhelming (8 sigma) is not the same as large. Suits a regime filter or a
   directional bias, not a standalone entry trigger.
3. **AUC is not a decision rule.** It measures ranking. A tradeable rule needs
   the hit-rate form: "when Δ PCR > X, what share of the next 30 min is up, and
   by how much" — then net of spread and fees.
4. **Regime split untested.** The window spans the Thursday→Tuesday expiry
   change (Sep-2025, detected in `expiry_calendar`) and several market regimes.

### The obvious integration

The scanner trades STOCK options but this is an INDEX signal, so the natural use
is as a **directional gate**: require the index Δ PCR to agree with a trade's
side before entering. That is a scalar knob, which means it can go through the
same insight → shadow-challenger → human-apply path as `target_pct`, rather than
being wired in by hand.

## REJECTED: trading the mean-reversion effect directly

`examples/mean_reversion_pcr.py` turned the strongest statistical finding into a
strategy — fade a 30-min drift extreme, take-profit exit, no trail. It **fails
out of sample and should not be traded.** Kept in the repo as a worked example
and so the result is not rediscovered.

| | in-sample (full period, default params) | out-of-sample (4 folds) |
|---|---|---|
| net P&L | ‑₹71,553 | **‑₹139,907** |
| fees | ₹130,084 | ₹60,347 |
| **gross (net + fees)** | **+₹58,531** | **‑₹79,560** |

**The gross P&L flips sign out of sample.** In-sample the edge paid before costs
and lost only to fees; out-of-sample it lost money before fees were even
charged. That is overfitting, not a cost problem, and it was foreshadowed by
unstable fold winners (target_pct 0.08↔0.18, drift_pct 0.08↔0.14 — a real
optimum would not swing like that).

Per-fold OOS: ‑33,276 / ‑44,156 / ‑50,499 / ‑11,977 (win rates 40.7–53.7%, so
the entries are not random — the payoff is simply too small).

**What this teaches, and it is worth keeping:**
1. **AUC 0.38 is too weak to be an entry trigger.** Statistically overwhelming
   (z ‑7, replicated on two indices) and still not tradeable once spread, fees
   and theta are charged. Significance and tradeability are different tests, and
   only the second one pays.
2. **The +25% take-profit does not transfer between instruments.** It was
   measured on stock options that pop 20–40%; NIFTY ATM options over 30 min need
   a ~1% index move to get there, so 69% of trades timed out instead. A constant
   derived on one instrument is not a constant.
3. **Fees dominate at this frequency.** ~₹65/round trip against ~₹29 of gross
   edge per trade. Any premium-BUYING strategy at multi-trade-per-day frequency
   starts in a hole this deep.

**Still valid, and unaffected by this rejection:** the exit fix (+₹79,653) was
measured on the ACTUAL live book with real fills, not on a hypothetical
strategy, and remains the highest-value change available.

## NOT VALIDATED: PBK Seller (premium selling)

`examples/pbk_seller.py` was the counter-test to the buying strategies — sell
premium behind a structure wall, which should fit a mean-reverting market and
put theta on our side. **It does not validate, but it fails differently and the
difference is informative.**

| | in-sample (2y) | OOS 4 folds | OOS 6 folds |
|---|---|---|---|
| net P&L | ‑₹20,679 | +₹8,778 | **+₹1,399** |
| positive folds | — | 2/4 | **3/6** |
| max drawdown | 7.84% | 0.45–2.28% | 0.38–3.39% |
| worst single trade | ‑₹5,847 | — | — |

Pre-registered pass bar was **4+ of 6 folds positive with no single fold
carrying the total**. Result: 3/6, and fold 3 (+₹9,872) alone exceeds the
+₹1,399 total. Parameters were stable in the 4-fold run (`otm_offset` 2 in all
four) but destabilised at 6 folds (`target_pct` 0.35↔0.65). Verdict: **roughly
break-even out of sample, i.e. no extractable edge after costs.**

**Why this result is still worth having:**
- **The tail risk everyone fears from naked selling did not appear.** Max
  drawdown 0.38–3.39% per fold and a worst trade of ‑₹5,847. The wall-based
  structure exits work. Break-even with 1–3% drawdowns is a sound chassis with
  no fuel — quite different from the mean-reversion strategy's ‑₹139,907.
- **It is not actually harvesting theta.** 76% of exits are `signal` (structure
  invalidation), only 19% reach target, and the win rate is 42.6% — a real
  premium seller wins 70–90%. This is a directional structure trade in a
  seller's clothing, so theta never gets to work.

## The pattern across everything tested

| Strategy | gross edge/trade | fees/trade | net/trade |
|---|---|---|---|
| Scanner-trader (live book) | — | ~₹65 | ‑₹930 |
| Mean-reversion buyer | +₹29 | ‑₹65 | ‑₹35 |
| PBK Seller | +₹28 | ‑₹57 | ‑₹30 |

Two strategies with nothing in common — one buying and one selling, one 30-min
and one structural — land within ₹1 of each other on gross edge, and both at
roughly **half** the cost of trading. That consistency is the real finding:

> **At intraday frequency the gross edge available to these strategies is
> ~₹28/trade against a ~₹57–65 cost hurdle. The gap is not closeable by
> parameter tuning, because it is not a parameter problem.**

What could close it: far fewer trades with much larger per-trade moves
(positional, where ₹57 is trivial against the move size); materially lower
costs; or a signal genuinely stronger than anything present in this data.
Sizing up improves the fee ratio but scales drawdown with it, so it buys
ratio, not quality.

## NOT VALIDATED: CPR+VWAP high-volume pullback

`examples/cpr_vwap_pullback.py`, from a chart observation: above CPR+VWAP buy
CALLs on a retracement, below buy PUTs, and the retracement should be quiet.

**The index-level study (183 sessions, both indices) produced two real,
4/4-replicated results** — worth keeping regardless of the strategy outcome:

1. **The pullback rule works.** Waiting for a retracement beat trend-only in
   every case: NIFTY BULL ‑0.0006→+0.0039%, BEAR +0.0117→+0.0162%; BANKNIFTY
   BULL +0.0050→+0.0089%, BEAR +0.0069→+0.0121% (win rates 50–54% → 53–57%).
2. **The volume rule is INVERTED from the textbook.** Low-volume pullbacks had a
   NEGATIVE mean in all four cases (‑0.0021 / ‑0.0040 / ‑0.0053 / ‑0.0129);
   high-volume pullbacks were the best bucket in all four (+0.0058 / +0.0200 /
   +0.0150 / +0.0223). A retracement nobody participates in is aimless drift.

**Priced as real options, it fails.**

| | NIFTY | BANKNIFTY |
|---|---|---|
| gross/trade (in-sample) | **‑₹9** | **+₹77** |
| fees/trade | ₹65 | ₹96 |
| net/trade | ‑₹74 | ‑₹19 |
| **OOS gross (3 folds)** | — | **+₹1,093 total** |

NIFTY has no gross edge at all — theta and spread consumed the index-level
signal. BANKNIFTY's in-sample +₹77/trade gross was the best of the session
(2.7× the other strategies), but out of sample it collapses to ~₹17/trade gross
across 63 trades, 1/3 folds positive, unstable params. Same overfitting
signature as the others.

Also note fees are NOT purely fixed: BANKNIFTY costs ₹96/trade against NIFTY's
₹65 because its premiums are ~2× larger and the STT/GST component scales. That
weakens the "amortise the fixed fee by trading bigger" argument.

## Three strategies, one signature

| Strategy | IS gross | OOS gross |
|---|---|---|
| Mean-reversion buyer | +₹58,531 | **‑₹79,560** |
| PBK Seller | (net ‑₹20,679) | (net +₹1,399, 3/6 folds) |
| CPR/VWAP pullback (BNF) | +₹31,560 | **+₹1,093** |

Every strategy is gross-positive in sample and gross-flat-or-negative out of
sample. Three genuinely different shapes — counter-trend buying, structural
selling, with-trend pullback buying — all collapse the same way. The consistent
conclusion is not that each rule was wrong, but that **the apparent edges are
in-sample artifacts, and what survives is smaller than the ~₹57–96 cost of
trading an intraday index option.**

What has NOT been tested: delivery/positional equity, where costs are ~0.25%
of position value instead of a flat ₹57–96 per trade. That is a different cost
regime, not another variant of the same one. `scripts/backfill_equity_daily.py`
exists to build that dataset.

## Reproduce

```bash
venv/bin/python -m scripts.journal_day [YYYY-MM-DD]      # one day's round trips
venv/bin/python -m scripts.exit_counterfactual --tps 10,15,20,25,30
venv/bin/python -m scripts.entry_retrace_study --window 30 --hold 60
venv/bin/python -m scripts.move_precursors --per-move    # needs more bias days
venv/bin/python -m scripts.index_chain_precursors --source option_bars \
    --underlying NIFTY --lookback 6        # index chain study (also BANKNIFTY)
```

All read live DBs through a temp copy of the file + WAL sidecars (`mode=ro`
fails on the live SQLite; the DuckDB store is write-locked by the service).
