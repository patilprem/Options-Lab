# FNO Stock Scanner — feasibility + plan

Goal: pull options / OI / volume / price data for the ~190 NSE FNO stocks,
scan for good **options-buying setups**, and aggregate the same data into a
**NIFTY / BANKNIFTY directional bias** signal.

## Implementation status (F1–F6 built)

All six phases are implemented on `claude/fno-options-data-plan-ggr16e`, gated
OFF by default (`scanner` setting) — inert until turned on with live Dhan creds
on the VPS during market hours. Offline unit tests cover every pure layer
(universe parse, buildup/volume, ranking, chain metrics, scoring, index bias +
accuracy, hit-rate, signal routing). Still pending real-market verification on
the VPS: quote-API batch limits/quotas, chain-gate contention under the full
shortlist, and the on-demand expired-options backfill for flagged names.

- **F1** `resolve_fno_universe()` + dated `fno_universe` table
- **F2** `app/engines/scanner.py` Tier-1 sweep + `stock_snapshots`
- **F3** Tier-2 shortlist deep-dive via the shared chain gate (deployed-first)
- **F4** `setup_score()` + `/scanner*` endpoints + Scanner UI
- **F5** `index_bias()` + accuracy scoring + `/scanner/index-bias` + bias cards
- **F6** `ctx.signal()` in all contexts + provider + `/scanner/validation` +
  on-demand backfill; example `examples/index_momentum_with_bias.py`
- **Auto-trader** (Route 2, `app/engines/scanner_trader.py`) — a positional
  PAPER book that trades the screener's own picks: buys the ATM option of the
  bias side on setups scoring ≥ entry_score, sizes to risk a fixed % of
  capital, holds across days with a ratcheting trailing stop, and exits on
  hard stop / trail / target / max-hold / setup-decay (score < exit_score or
  bias flip). Reuses `engines/fills.py` (cost model, invariant #2) and the
  registry ledger (mode PAPER, id "SCANNER"); runs each Tier-2 cycle, parallel
  to the Strategy/paper engine and never touching it. Endpoints
  `/scanner/trades` + `/scanner/trade-settings`; Scanner UI shows the live
  book. Gated OFF (`scanner_trade` setting).
- **Trade journal + insights** — every auto-trader entry/exit also writes a
  rich row to SQLite `scanner_journal` (full setup context at entry: score
  reasons, buildup, Tier-2 chain state, option quote/IV/spread, config; at
  exit: prices, fees, reason, MFE/MAE premium excursions, hold time, score at
  exit — each exit row is a self-contained round trip).
  `engines/journal_insights.py` (pure, tested) aggregates closed trades into
  win rate / expectancy by score band / entry hour / buildup / exit reason,
  trail giveback, churn re-entries — and derives evidence-backed SUGGESTIONS
  (raise entry_score, tighten trail, confirmation entry, re-entry cooldown…),
  each gated on minimum samples so small-N noise never becomes advice. It
  proposes, never mutates settings. Endpoints `GET /scanner/journal` +
  `GET /scanner/insights`; Scanner UI shows the journal + suggestions; after
  a close, at most once a day, the top suggestions are also recorded as
  scanner events (Activity view) so reflection is proactive, not on-demand. This is the honest home for
  "trade whatever high-probability stock the screener finds" — a single
  Strategy can't hop between underlyings, so the multi-symbol book is a
  dedicated engine, not a Strategy subclass.

## Verdict: feasible on the current stack

The VPS, FastAPI+asyncio process, and DuckDB are NOT the constraint — Dhan's
API rate limits are. Sized honestly:

| Resource | Load | Verdict |
|---|---|---|
| CPU/RAM | ~190 quote rows/min parsed + DuckDB SQL aggregates | trivial |
| Disk | ~5–15 MB/day of snapshots (see estimates below) | years of headroom |
| Option Chain API | 1 unique req / 3 s (already globally gated in MarketHub) | **binding** — full-universe sweep = ~10 min |
| Market Quote REST | batch quotes, ~1 req/s (`quote_data` in the SDK) | one call covers the whole universe |
| WS feed | Dhan MarketFeed allows thousands of instruments/connection | plenty (verify exact cap in docs) |

The trap to avoid: polling 190 option chains continuously. At 1 req/3 s a full
sweep takes ~9.5 min and would starve the existing chain poller that paper
fills depend on. The design below never does that.

## Design: two-tier scanner

**Tier 1 — broad & cheap (whole universe, every minute).**
One batched Market Quote call for all FNO stock **futures** (+ spot) returns
LTP, OHLC, volume, and futures OI for ~190 names in a single request.
From consecutive snapshots compute, per stock:
- price %change, range expansion, distance from day high/low
- volume surge vs trailing N-day same-time-of-day baseline (DuckDB SQL)
- futures OI delta → classic buildup classification:
  price↑ OI↑ long buildup · price↓ OI↑ short buildup ·
  price↑ OI↓ short covering · price↓ OI↓ long unwinding

Rank and keep a **shortlist of ~10–20 movers**.

**Tier 2 — deep & narrow (shortlist only, every ~5 min).**
Poll the option chain ONLY for shortlisted stocks, through the existing
`MarketHub._chain_gate` (3 s global spacing), with deployed-strategy
underlyings taking priority in the queue. Per shortlisted stock derive:
- PCR (OI + volume), ATM IV and IV percentile vs recorded history
- strike-level OI concentration and intraday OI shift (support/resistance walls)
- IV skew (call vs put wing), delta-adjusted option volume
- **liquidity screen**: bid-ask spread % and OI floor — stock options outside
  the top ~40 names are illiquid; a "great setup" you can't exit isn't one.

**Setup score** (options-buying bias): e.g. long buildup + volume surge +
rising ATM/OTM call OI with price confirming + IV not already spiked +
spread below threshold → CE-buy candidate (mirror for PE). Scores land in an
API endpoint + UI table + ntfy alert above a threshold.

**Index bias (NIFTY / BANKNIFTY).**
Aggregate Tier-1/Tier-2 signals into breadth metrics:
- % of FNO universe (and of index constituents, weighted) in long vs short buildup
- advance/decline + volume-weighted breadth of constituents
- aggregate stock-level PCR and cumulative futures OI flow, sector-bucketed
  (bank names → BANKNIFTY bias)
→ a bias score with its inputs, recorded every cycle. Framed honestly: it is a
**regime/bias indicator to be validated against recorded outcomes first**, not
a prediction oracle. We record bias vs realized index move from day one so its
hit-rate is measurable before anyone trades on it.

## Daily API + storage budget

- Tier 1: 1–2 batched quote calls/min ≈ 400–750 calls/day.
- Tier 2: 15 chains × (1 req/3 s shared gate) = ~45 s per sweep, every 5 min.
- Optional full-universe chain sweeps (~10 min each) 2–3× daily at fixed times
  (post-open, mid-session, pre-close) for the EOD footprint dataset.
- Storage: futures snapshots 190 × 375 min ≈ 71k rows/day; shortlist chain rows
  ≈ 40–60k/day → single-digit MB/day in DuckDB. `chain_snapshots` schema
  already fits stock chains as-is (underlying = stock symbol).

## Phases (each sized for one focused session)

### F1 — FNO universe + instrument master
> Extend app/data/dhan_client.py with resolve_fno_universe(): from the cached
> scrip master, build {symbol → spot security_id, current-month future id,
> fno_segment, lot_size, expiry dates} for all NSE FNO stocks; cache like
> resolve_mcx_ids does. Persist to a DuckDB `fno_universe` table (dated, so
> lot-size history accumulates — same concern as LOT_HISTORY). Offline test
> replays a trimmed scrip-master fixture.

### F2 — Tier-1 bulk quote poller
> New app/engines/scanner.py: an asyncio task (started like the chain poller)
> that batch-fetches futures+spot quotes for the whole universe once a minute
> via the SDK quote API, market-hours gated, persisting to a new
> `stock_snapshots` DuckDB table. Pure functions for buildup classification and
> volume-surge vs N-day baseline (DuckDB window SQL). Offline tests replay a
> saved quote response. Verify actual batch-size/rate limits against Dhan docs
> during implementation.

### F3 — Tier-2 shortlist chain deep-dive
> Shortlist ranker (pure) + chain polling for shortlisted stocks through the
> EXISTING MarketHub chain gate with deployed underlyings prioritized — never
> starve paper fills. Persist to chain_snapshots. Derive PCR/IV/OI-shift/skew
> and the liquidity screen. Optional scheduled full-universe sweeps 2–3×/day.

### F4 — Setup scoring + Scanner UI
> Composite setup score with per-component reasons; GET /scanner (ranked
> table), GET /scanner/{symbol} (detail); frontend Scanner nav view; ntfy
> alert + registry event when a score crosses threshold. Thresholds in
> settings.

### F5 — Index bias panel
> Breadth aggregation (constituent-weighted for NIFTY/BANKNIFTY) → bias score
> recorded every cycle with its inputs; GET /scanner/index-bias + UI panel on
> the Scanner view; nightly job scores yesterday's bias vs realized index move
> so accuracy is tracked from day one.

### F6 — Validation before trading
> Only after 3–4 weeks of recorded scanner data: measure setup-score hit-rates
> (forward returns of flagged options at 15/30/60 min), backfill expired
> option data ON DEMAND for flagged names only (full-universe options backfill
> is prohibitively slow), and only then expose scanner signals to strategies
> via Context — a new ctx.scanner read API, updated in all three contexts +
> loader smoke context + prompts/strategy_prompt.md (invariant #5).

## From scanner data to strategies

A strategy here is a single-underlying class behind Context (contract.py), so
"the scanner found RELIANCE" cannot live inside one strategy that hops between
stocks. Three routes, in the order to build them:

**Route 1 — index strategies on the bias signal (first, cheapest).**
NIFTY/BANKNIFTY already have feed, liquidity, and backtest data. Expose the
F5 bias through a new Context read (e.g. `ctx.signal("index_bias")` returning
score + components, None in engines that lack it); an index option-buying
strategy then trades its own price action ONLY when the breadth bias agrees
(e.g. ORB long CE only if bias > +0.6). Invariant #5 applies: implement in all
three contexts + loader smoke context + prompts/strategy_prompt.md.

**Route 2 — scanner-deployed template strategies on stocks.**
The scanner picks the stock; the platform instantiates a parameterized
template on it (StrategyMeta.underlying = that stock, entry thesis passed via
params). Start with one-click deploy from the Scanner UI (human confirms);
auto-deploy only after the template has a measured paper track record.
Templates to write first:
- *Momentum CE/PE buyer*: enter on a confirming bar (don't buy the alert —
  buy the first bar that continues it), ATM or 1-OTM via strike_offset,
  engine-declared sl_pct/target_pct, time stop by 14:45, skip if ATM IV
  already spiked vs its recorded percentile or spread > threshold.
- *OI-wall breakout buyer*: enter when price closes beyond the max-OI strike
  with OI unwinding there (shorts trapped), same exit discipline.

**Route 3 — signals inside stock strategies.**
Once a stock strategy exists, `ctx.signal(...)` also serves its own
underlying's Tier-2 metrics (setup score, PCR, IV percentile, OI shift) so
LLM-generated stock strategies can use them as filters.

**Sizing and exits for buying** (all routes): risk a fixed % of allocated
capital per trade — lots = risk_budget / (premium × sl_pct × lot_size);
always declare sl_pct/target_pct so the engine enforces them; prefer time
stops (theta bleeds all day) and trail with set_levels after 1R.

**Backtesting honesty:** scanner signals derive from chain_snapshots, which
only accumulate FORWARD — there is no historical intraday stock-chain data to
replay. So: signal replay + forward-return stats over the recorded window
(F6), entry/exit pricing via on-demand expired-options backfill for flagged
names, then paper trading as the real proving ground. A conventional
multi-year backtest of scanner strategies is not possible and should not be
faked.

## Risks / gotchas
- **Chain-gate contention** is the real engineering risk — priority queue for
  deployed underlyings is non-negotiable.
- Stock option **liquidity**: hard spread/OI screens before any buy signal.
- Frozen-chain problem already solved for indexes (fingerprint dedup) applies
  to stocks too — reuse it.
- Lot sizes per stock change via exchange circulars — the dated
  `fno_universe` table mirrors the LOT_HISTORY approach.
- Corporate actions distort stock price/volume baselines; flag symbols with
  overnight gaps > X% and reset their baselines.
- Verify Dhan quote-API batch limits and any daily quota on the option-chain
  endpoint against current docs before F2/F3 — numbers here are from docs as
  of writing, and Dhan has churned limits before.
