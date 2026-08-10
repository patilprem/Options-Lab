# OptionsLab — project memory for Claude Code

Personal options-strategy platform for Indian markets (NSE/BSE/MCX) on
DhanHQ v2 APIs. One FastAPI process, SQLite (app state) + DuckDB (market
data), dark-terminal dashboard in a single static HTML file. Single
user; runs on a small VPS behind Tailscale.

## Build & run

**Backend only** (existing Python setup):
- `python3 -m venv venv && venv/bin/pip install -r requirements.txt`
- `venv/bin/uvicorn app.main:app --reload` → FastAPI at http://localhost:8000/docs

**Full stack** (with React frontend):
- `cd frontend && npm install && npm run build` (creates app/static/)
- Then run the backend as above; dashboard at http://localhost:8000/

**Development** (hot reload):
- `cd frontend && npm run dev` → Vite at http://localhost:5173 (proxies /strategies etc to :8000)
- Run backend in another terminal

**Deploy:** pushing to GitHub `main` IS the deploy. The VPS runs
`optionslab-autopull.timer` (deploy/autopull.sh, installed once via
deploy/install_autopull.sh): every 5 min it fetches origin/main and, when it
changed, redeploys via deploy/deploy.sh (deps + UI build + offline tests gate
the restart). Restarts are deferred during IST market hours (Mon–Fri
09:00–23:30, covering NSE/BSE close at 15:35 AND MCX's later close — a
restart mid-session drops the live feed/chain recording, and MCX chain data
can't be re-fetched afterward); `FORCE=1 bash deploy/autopull.sh` overrides,
and a commit message containing `[force-deploy]` overrides remotely (tests
still gate the restart). So: ordinary changes land on `main` and go live at
the next
off-hours window; genuinely urgent fixes get the marker.

## Run / test
- **Backend:** `python3 -m venv venv && venv/bin/pip install -r requirements.txt && venv/bin/uvicorn app.main:app --reload` → API at `http://localhost:8000/docs`
- **Frontend (dev):** `cd frontend && npm install && npm run dev` → dashboard at `http://localhost:5173`, proxied to backend
- **Frontend (prod):** `cd frontend && npm run build` → outputs to `app/static/`; FastAPI serves it
- Python tests: `venv/Scripts/python -m pytest tests/ -q` (offline; no creds/network — Dhan SDK is lazy-imported). Covers Dhan parsing (`test_dhan_parsing.py`) and the live-feed candle builder + hub wiring (`test_feed.py`).

## Architecture map
- `app/core/contract.py` — THE strategy interface (Strategy, Context,
  LegSpec, Position). Strategies are LLM-generated code that must subclass
  Strategy; they touch the world only through Context.
- `app/core/loader.py` — AST-scans pasted code (bans os/network/eval),
  compiles in a restricted namespace, smoke-tests. Not a true sandbox.
- `app/core/registry.py` — SQLite: strategies + lifecycle state machine
  (DRAFT→VALIDATED→DEPLOYED_PAUSED⇄RUNNING→STOPPED), daily_pnl (keyed by
  mode), trades blotter, events log, settings, param overrides,
  paper_state (M4: per-strategy live-session snapshot for restart recovery),
  and two trade JOURNALS: scanner_journal (rich scanner-trader entries/exits)
  + strategy_journal (closed Strategy round trips) that back the insights
  engines.
- `app/engines/attribution.py` — entry-context capture (data state at entry,
  identical across contexts) + `build_round_trip` (a closed Position →
  self-contained journal record incl. MFE/MAE) + `attribution` (win-rate/P&L
  bucketed by an entry_context key).
- `app/engines/strategy_insights.py` + `journal_insights.py` — PURE trade-log
  analytics: aggregate closed trades (win rate, expectancy, breakdown by exit
  reason / entry hour / entry data-state, MFE-giveback, churn) into
  evidence-backed SUGGESTIONS, each gated on a minimum sample so small-N noise
  never becomes advice; they PROPOSE, never mutate settings. strategy_insights
  feeds backtest run results + `GET /strategies/{id}/insights` (paper) + a
  once-a-day proactive event; journal_insights is the scanner-trader twin
  (`GET /scanner/insights`). Position.mark() tracks MFE/MAE every bar.
- `app/engines/adaptation.py` — SAFE self-tuning for the scanner-trader
  (champion-challenger). Insights only NOMINATE; a param change must pass:
  persistence (rule fires ≥3 distinct days) → shadow trial (challenger config
  trades a VIRTUAL book on the same live scores/quotes — out-of-sample by
  construction, ledger untouched) → beat the champion's expectancy by a real
  margin over ≥14 days → human Apply (Scanner UI banner /
  `POST /scanner/proposal/apply`). Applies are ONE bounded step inside hard
  clamps, start a 21-day embargo, and are measured vs the pre-change baseline
  (a 'worse' verdict surfaces a revert warning). Discarded/dismissed rules
  cool down 30 days. NEVER auto-applies — the ONLY settings-mutation path is
  apply_proposal(). Strategies get the same discipline via walkforward.py
  (derive on IS, validate OOS), not a shadow book. HOLISTIC coverage: every
  journal-insight rule with a scalar-izable remedy has an ADAPTABLE mapping —
  incl. the behavioural gates reentry_cooldown_min / entry_cutoff_min /
  fresh_buildup_only, which live in TradeConfig (defaults = off/no-op) and are
  enforced inside pick_entries(), the single choke point both champion and
  shadow challenger enter through, so a challenger override trials them with
  no extra wiring. Only `fast_hard_stops` stays human-only (its remedy — a
  confirmation entry — is a state machine, not a scalar).
- `app/engines/walkforward.py` `adaptive_search` + `app/engines/strategy_adapt.py`
  — the Strategy twin of the scanner adaptation loop. Persistence (an insight
  fires ≥3 distinct days) ARMS a scan; the scan builds ONE-step bounded param
  neighbours, SELECTS the best per fold IN-SAMPLE and REPORTS out-of-sample
  (never selects on OOS), and only proposes the modal IS-winner if it's
  preferred in a majority of folds AND beats current params OOS on BOTH the
  metric and realized P&L. Human Apply sets ONE param override, starts a 21-day
  embargo, and is measured forward on the strategy_journal vs the pre-apply
  baseline. Endpoints `GET /strategies/{id}/adaptation`,
  `POST /strategies/{id}/adaptation/{scan|apply|dismiss}`; PaperPanel shows the
  proposal / armed-scan / embargo states. The scan is heavy (many backtests) so
  it's human/schedule-triggered, never in the trading loop.
- `app/engines/fills.py` — shared fill simulation, Indian option charges
  (brokerage/STT/txn/GST/SEBI/stamp), SL/target level helpers, rough
  margin estimate (estimate_margin now takes a per-underlying `factor`).
- `app/engines/margin.py` — M5 real margin: `real_margin` sums Dhan's per-leg
  `margin_calculator` across a structure (5min cache) and falls back to the
  calibrated `estimate_margin` on any failure / no client / synthetic. Paper
  enter uses it; backtests use estimate×`underlying_factor` (settings
  `margin_factor:<u>`). Equity margin live-verified; FNO SPAN needs market hours.
- `app/engines/backtest.py` — event-driven replay from the store;
  date-by-date P&L; enforces declared stop/target each bar; margin policing
  scaled by the calibrated per-underlying factor.
- `app/engines/walkforward.py` — M6: K-fold walk-forward. Splits a range into
  folds (IS/OOS by is_frac), grid-searches params on each IS window (reuses
  run_backtest), evaluates the winner OOS, chains an aggregate OOS equity
  curve. Caps runs (max_runs), progress via callback. Pure — API builds the
  strategy factory. Frontend Walk-Forward tab (components/WalkForward.jsx).
- `app/engines/watchdog.py` + `recording_watchdog.py` — two ntfy watchdogs on
  the once-a-minute hub loop. FeedWatchdog watches the SOCKET (down/silent);
  RecordingWatchdog watches whether ROWS LAND (`store.recording_health()`),
  because the 07-23..27 outage never touched the socket. Segment-aware and
  event-table-aware so idle-but-correct tables never cry wolf; one push on
  state change, re-push every 15 min, one all-clear.
  The chain poller also SELF-HEALS now (`MarketHub._chain_stall_step`, pure
  ladder in `chain_stall_stage`, driven by that same once-a-minute loop). It
  had frozen SIX times (07-23..27, 07-28, 07-29, 07-30, 07-31, 08-03) and every
  fix only made it more VISIBLE — task alive, cycles completing in ~20s,
  requests going out, cache not moving. Tracking when each underlying's chain
  cache last CHANGED (not when it was last persisted — `_chain_persisted_fp`
  can't tell a dead poller from an idle recorder) drives escalating remedies
  while its session is open: 3min → rebuild the Dhan client (the sustained
  failure mode, DhanEmptyFailure, is swallowed by design, so the one path that
  killed a session on 07-13 was the one path that never rebuilt); 6min → also
  drop the cached expiry list (07-23..27's root cause); 9min → escalate with
  sid/segment/expiries in the line, deliberately BEFORE the 15-min push so the
  phone alert lands with a diagnosis attached. Each stage fires once per
  episode and movement re-arms the ladder; movement ELSEWHERE (a stock
  deep-dive still ticking) is the holiday discriminator — selective failure =
  `error` AND an ntfy push carrying the detail (the bare 'NOT RECORDING' push
  has left an SSH session as the only next step six times running), everything
  frozen = `warn`, no push. Stage 3 is NOT terminal: both remedies re-apply
  every 10min until the cache moves or the session closes — 08-03 had NIFTY
  recover on a restart while BANKNIFTY/CRUDEOIL/GOLD stayed dead another half
  hour with every stage spent and nothing left that would touch them. The
  ladder advances ONE step per pass, so a late pass can never escalate to a
  human without having tried the remedies first.
  ROOT CAUSE, CONFIRMED 08-03 (supersedes the earlier "selective failure /
  monthly-only expiry" theory, which the full event log refutes — 08-03 only
  LOOKED selective because NIFTY's last success fell inside the 15-min alert
  window): the DHAN CLIENT GOES BAD and every option_chain call then returns
  `{'status':'failure','remarks':{all None},'data':''}` for EVERY underlying
  and EVERY expiry — NIFTY 08-04+08-11, BANKNIFTY 08-25, CRUDEOIL 08-17, GOLD
  08-31, 90 minutes straight — while ticks keep flowing and underlying_bars
  keeps writing. It never self-recovers. A process restart fixed all four at
  once; later the same day an automatic stage-1 client rebuild fixed GOLD in
  60s. `_poll_cycle` DID always have a rebuild path, but only on a RAISED
  exception, and `_fetch_chain_ratelimited` swallows DhanEmptyFailure by
  design — so the one failure mode that actually kills a session was the one
  that never triggered a rebuild. That is exactly what stage 1 now fixes.
  Corollary: the holiday discriminator must NOT be "is another chain moving?"
  (that would have silenced this outage). A holiday still SERVES a frozen
  chain — fetches succeed, the cache just stops changing — so what a holiday
  looks like from here is a SILENT FEED. `_chain_dead_event` judges on
  `feed_status()['tick_age_sec']` first.
  Separately (FIXED, do not conflate with the outage): `CHAIN_TARGETS` asks
  every core name for `("WEEKLY", 0/1)`, but BANKNIFTY's weeklies were
  discontinued by NSE and MCX options are monthly-only, so those three cached
  and persisted their MONTHLY chains under `expiry_kind="WEEKLY"` and never
  recorded a second expiry. `chain.has_weekly_cycle`/`effective_targets` now
  derive the kind FROM THE EXPIRY LIST (no hardcoded name list to go stale) and
  remap WEEKLY→MONTHLY when there is no weekly cadence — fixing the label AND
  making offset 1 a real next-month expiry. MONTHLY 0 resolves to the same
  contract WEEKLY 0 did, so it re-labels without re-pointing.
  `MarketHub._effective_leg` translates a still-WEEKLY LegSpec on those names so
  running strategies keep pricing the same contract; `scripts/
  relabel_chain_expiry_kind.py` fixes the history (data-driven both ways —
  BANKNIFTY genuinely HAD weeklies pre-Nov-2024, so a blanket relabel would
  corrupt them; dry-run default, Parquet backup + readback, PK-collision check).
  AFTER RUNNING IT: strategies/backtests declaring WEEKLY on those underlyings
  read an EMPTY series from the store — change the declaration to MONTHLY.
- `app/engines/risk.py` — M7 risk panel: pure `evaluate()` (portfolio max daily
  loss + per-strategy caps from settings), `exposure()` by underlying/expiry,
  `snapshot()` (margin utilization, day P&L vs max-loss). PaperRunner.enforce_risk
  runs it EVERY BAR — auto-pauses breached strategies (per-strategy cap) and ALL
  on portfolio breach, with 'risk' events. GET /risk + POST /risk/settings;
  frontend Risk nav view (pages/RiskView.jsx).
- `app/engines/live.py` — M8 live execution (GATED, parallel to paper — never
  touches paper paths). LiveContext routes enter/exit through Dhan super orders
  (server-side SL) → LIVE ledger (mode="LIVE"); LiveRunner honors risk caps and
  arms Dhan's kill_switch on portfolio breach. 5 gates before any real order:
  live_enabled + live_dry_run==off + static IP + per-strategy checklist ack +
  (market hours, lots<=live_max_lots, risk). make_order_client() returns a
  DryRunOrderClient unless fully enabled (default = dry-run, logs not sends).
  Endpoints: /live/status, /live/settings, /live/kill, /strategies/{id}/live/ack,
  deploy_live, live/{play|pause|stop}. Frontend LiveModal checklist gates deploy.
  A real market-hours run is the remaining step.
- `app/engines/order_updates.py` — M8 FILL RECONCILIATION. LiveContext books a
  position the instant an order is ACCEPTED, at the LIMIT price it asked for —
  intent, not fact. Every order is now tracked to a terminal broker status and
  the ledger corrected: real avg price, real qty, a rejected entry's position
  DROPPED, a rejected EXIT's position REOPENED (the ledger reading flat while
  the broker still holds it is the worst divergence there is), and a partial
  exit SPLIT so the unfilled units don't vanish. Pure `parse_order_update` /
  `reconcile` + `FillReconciler` (in-flight book, dedupes replays, sweeps orders
  with no terminal update after STALE_S) + `LiveOrderFeed` (dhanhq `OrderUpdate`
  socket on its own thread, own reconnect — the SDK's `connect_order_update()`
  just returns when the socket drops). BOOK-THEN-CORRECT, never wait-for-fill:
  waiting on a confirmation that may never arrive would lose positions the
  broker really opened. So an unreconciled ledger is the degraded mode and it is
  VISIBLE — `/live/status` → `fills.reconciled: false`. Corrections happen ONLY
  on the terminal update (PART_TRADED's filled qty keeps moving; acting on both
  would double-apply). `registry.amend_trade` corrects the blotter row IN PLACE
  — one row per fill, so a correction must never append a second.
  VPS-PENDING: only `Data.orderNo`/`Data.status` are confirmed against the
  installed SDK; the filled-qty and avg-price field names are not in it, so the
  parser accepts every spelling Dhan's REST order object uses. Run one real
  order through it and watch for the `unparseable order update` warn.
- `app/engines/paper.py` — MarketHub (shared feed) + PaperContext +
  PaperRunner (asyncio task per strategy, play/pause). M4: PaperContext
  persists open positions + margin/P&L to registry.paper_state on every
  fill/close, day-end, and a 60s PaperRunner heartbeat; on startup
  runner.restore_all re-deploys RUNNING/DEPLOYED_PAUSED strategies and
  restores same-session positions (logs 'recovered N positions'). MarketHub drives a
  real dhanhq MarketFeed via `app/engines/feed.py` (or a synthetic replay
  under OPTIONSLAB_SYNTHETIC=1 / synthetic store / no creds); strategies
  register (underlying, timeframe) and receive per-timeframe
  ("bar", underlying, interval, Bar) messages.
- `app/engines/feed.py` — CandleBuilder (pure tick→OHLC, bucket-start
  labeled) + LiveFeed (dhanhq MarketFeed on a dedicated thread — the SDK
  builds its own loop; bridges ticks to the app loop via call_soon_threadsafe;
  reconnect w/ exponential backoff; connect/disconnect → registry.record_event).
  Subscribes index SPOT only (IDX/Ticker).
- `app/engines/chain.py` — pure option-chain normalizer: live chain
  (double-nested at `resp['data']['data']={last_price,oc}`) → ATM-relative
  OptionQuote dict keyed (expiry_kind, expiry_offset, strike_offset,
  option_type) with real ltp/bid/ask/iv/oi/greeks; + resolve_expiry
  (weekly/monthly from expiry_list). MarketHub runs the poller (≥3s/req,
  global gate), caches quotes, and `hub.quote` serves them to fills (fallback
  to store). `hub.persist_chain_snapshots` writes snapshots into option_bars
  (used by the MCX recorder).
- `app/data/sessions.py` — THE canonical exchange session windows
  (NSE 09:15–15:30, MCX 09:00–23:30). Every underlying_bars write goes
  through `in_session()`: the live tick gate (paper._tick_ok), the live-bar
  upsert, AND the backfill. Guarding only ticks was not enough — the nightly
  gap repair wrote post-close phantom candles straight into the table.
  Unknown underlyings get the stricter NSE window.
- `app/data/store.py` — DuckDB schema (underlying_bars, option_bars with
  ATM-relative keys) + SyntheticStore fallback.
  READS DO NOT TAKE THE WRITE LOCK. `_lock` guards writes only; `_q`/`_q1` use
  a per-thread cursor (`_read_con`) — a separate connection over the same DB
  reading an MVCC snapshot. One shared lock used to stall every reader for as
  long as the biggest write took: measured 2026-08-11, /data/health flat at
  0.117s then an 18–30s stall every 5m56s, exactly the chain recorder's
  cadence (~4,500-row INSERT OR REPLACE with PK maintenance on the ARM VPS).
  `scripts/diag.py` (10s HTTP timeout) reported RED against a healthy
  recorder. The dashboard was the visible casualty; the one that mattered is
  that `hub.quote`/`quote_position` fall back to the store on a chain-cache
  miss, so a stop-loss check could queue behind the recorder for half a
  minute. Verified before building: during a 198s write, 3,558 cursor reads
  completed (median 4ms, max 116ms, 0 errors); cursors created mid-write are
  equally safe. Statements auto-commit, so only an IN-FLIGHT write is
  invisible to a reader. tests/test_store_read_concurrency.py pins it by
  holding `_lock` explicitly (no wall-clock race) and fails — never hangs —
  if `_q`/`_q1` take the lock again. dhan_client's row upserts went straight
  at `store.con`, unsynchronised against the writers inside DataStore; they
  route through `bulk_write` now.
- `app/data/dhan_client.py` — dhanhq SDK wrappers + backfill CLI.
  Live-verified 2026-07-09. Real-response quirks: intraday arrays under
  `data`, but expired options double-nest at `data['data']['ce'|'pe']` (only
  the requested side populated); `expiry_code` is 1-indexed (0 rejected), so
  store `expiry_offset` → API `expiry_code = offset+1`. Epoch→IST at the store
  boundary. Fetch split from parse so `tests/test_dhan_parsing.py` replays
  real trimmed sample responses offline (no SDK/creds). Creds from
  DHAN_CLIENT_ID/DHAN_ACCESS_TOKEN env or token_manager's managed token.
- `app/core/token_manager.py` — 24h token lifecycle: 08:30 IST check,
  ntfy push of login link, /dhan/callback capture, /token/status.
- `app/api/strategies.py` — all REST endpoints incl. /portfolio/today,
  /activity, /data/coverage, /trades, calendar/params/montecarlo; incident
  pair: POST /{sid}/wipe_day (erase a bad-quote paper day) + POST
  /{sid}/manual_trade (re-book the day's round-trip at actual prices;
  fees via engines/fills.py, realized net of fees).
- `frontend/` — THE canonical React + Vite SPA (JavaScript/JSX, React 18,
  Vite 5, Chart.js). (`ui/`, a parallel TypeScript copy, is DEAD — it builds
  to `app/static/dist/` which main.py does not serve. Ignore/remove it.)
  - `frontend/src/App.jsx` — root app: state, routing, `API.call` fetch helper
  - `frontend/src/components/` — Header, Nav, Summary, StrategyList,
    NewStrategyModal, DeployModal, LiveModal, Toast, and the StrategyDetail
    tab panels: PaperPanel (live /performance), BacktestPanel (defaults dates
    to /data/coverage), WalkForward (param sweep + OOS equity chart)
  - `frontend/src/pages/` — PositionsView, ActivityView, DataView,
    HistoryView, StrategyDetail
  - `frontend/src/styles/globals.css` — design system (colors, fonts; dark theme)
  - `frontend/vite.config.js` — dev proxy to backend:8000; **build outputs to
    `app/static/`** (exactly what main.py serves via StaticFiles)
  - **Build:** `cd frontend && npm run build` → `app/static/`; FastAPI serves it
- `prompts/strategy_prompt.md` — template given to an LLM to generate
  conforming strategies. Update it whenever the Context API changes.

## Invariants — do not break
1. PAPER and LIVE are separate ledgers. Every daily_pnl row and trade
   carries a mode; never sum across modes; never display a combined ₹.
2. Backtest and paper MUST share fill/fee logic (engines/fills.py) so
   results are comparable. Never fork the cost model per engine.
3. Strikes are ATM-relative everywhere (LegSpec.strike_offset). Absolute
   strikes only exist at fill time (Position.strike).
4. The engine enforces declared stop_loss/target every bar as a safety
   net, independent of strategy code. Keep that in any refactor.
5. If the Context interface changes: update ALL THREE contexts (backtest,
   PaperContext, LiveContext), the smoke context in loader.py,
   prompts/strategy_prompt.md, and the example.
6. A crashing strategy auto-pauses; it must never kill the process.
7. Timestamps are IST for anything user-facing; Dhan historical APIs
   return epoch — convert at the boundary (store layer).
8. UI is one static file; server-rendered nothing; keep zero build step.

## Domain gotchas
- MCX CONTRACT CHOICE IS BY OPEN INTEREST, not by nearest expiry
  (`dhan_client.pick_active_contract`). Liquidity rolls WEEKS before expiry:
  2026-08-04 GOLD resolved to GOLD-05Aug2026-FUT — expiring the next day, with
  the market long since on Oct — so its WS printed no ticks for 75+ min and its
  chain served a last_price frozen at 142204 for six hours. Both GOLD's
  underlying_bars AND its chain_snapshots recorded a contract nobody traded.
  CRUDEOIL survived the same code path only because its front month was still
  15 days out, so 'nearest' and 'active' coincided. OI (not volume) decides —
  volume is ~0 for everything pre-open, exactly when the roll matters. Falls
  back to nearest-expiry when quotes are unavailable, so the resolver still
  works offline from the cached CSV. Re-checked DAILY (main.py), not only after
  expiry, or a long-lived process freezes the pick for a whole cycle.
- Dhan intraday history: 90 days/call; expired options: 30 days/call,
  ATM-relative, NSE/BSE only (record MCX chain snapshots ourselves).
- Option Chain API rate limit: 1 unique request per 3 seconds.
- Access tokens last 24h (SEBI); static IP must be registered with Dhan.
- Lot sizes are a DATED table (backtest.py LOT_HISTORY + lot_size_on();
  NIFTY 25->75 Nov-2024 ->65 Jan-2026). Contexts expose date-aware
  .lot_size. On a new exchange circular: add a row + tests/test_lots.py.
  Expiry weekdays have changed historically.
- STT/charges rates in fills.py are approximations — verify vs contract
  notes before trusting absolute P&L.
- EVENTS TIMESTAMPS use a SPACE separator (`registry.record_event` stores
  `isoformat(sep=" ")`). A SQL filter built as `ts >= date('now')||'T09:15'`
  compares `' '` (0x20) against `'T'` (0x54) and silently excludes EVERY row —
  an entire day of ad-hoc diagnostics returned empty because of this
  (2026-07-28). Filter with `ts LIKE 'YYYY-MM-DD %'` (as `events_for` does) or
  use `scripts/diag.py`, which has the correct forms baked in.
- LONG-RUNNING LOOPS must have their ENTIRE body inside a try
  (tests/test_loop_resilience.py enforces this structurally). The chain poller
  died silently on an unguarded statement (2026-07-28) and five sibling tasks
  had the same hole. Also: any iteration over `_wanted`/`_chain_only`/
  `_builders` must snapshot first — the scanner mutates them concurrently.
- ONE RANKING, ONE GATE: `StockScanner.ranked_scores()` is THE trading
  surface. The Tier-2 shortlist (rank_for_deep_dive), the entry gate
  (pick_entries) and ranked_scores all use setup_score, recomputed fresh per
  call (tests/test_shortlist_alignment.py enforces alignment). Two formulas
  drifted apart once (2026-07-28: every qualifying name lacked chain data,
  trader silently skipped them all) — don't reintroduce a second scale.
- INDEX VOLUME (verified 2026-07-27, don't re-derive): `underlying_bars.volume`
  for NIFTY/BANKNIFTY is POPULATED from 2025-11 onward and ZERO before it.
  An index has no volume of its own, so Dhan's intraday historical substitutes
  the front-month future's — the SAME series the live companion records
  (day totals 261–382M, avg ~4M per 5-min bar, both writers agree). So the
  nightly gap repair overwriting live companion bars is benign, NOT a mix of
  two definitions. The companion still matters: it's the only volume available
  DURING the session, before any repair runs.
  Pre-2025-11 (~325 NIFTY sessions back to 2024-07) has no volume and cannot
  get any — `scripts/backfill_index_volume` correctly REFUSES those dates
  because their front-month contracts have left the scrip master, and a
  resolved-but-wrong contract would write plausible-looking thin volume.
  Price/OHLC for those sessions is fine; only volume is absent.

## Data / price-action strategy surface (post-M8)
Strategies now reach a richer, backtestable data surface (all through Context):
- `ctx.signal("index_bias"|"tier2")` REPLAYS recorded point-in-time data in
  backtest (index_bias_history / chain_snapshots via engines/replay.py), None
  when unrecorded; tier1/setup stay None. Live/paper unchanged.
- `indicators.*` — a tested toolbox (engines/indicators.py) injected into the
  strategy namespace (EMA/RSI/ATR/ADX/Supertrend/Bollinger/MACD/VWAP + price
  action: swings, BOS, inside/outside bar, prev-day, opening range, pivots,
  CPR, gap). Strategies must NOT hand-roll these.
- `ctx.history(n, interval=60)` multi-timeframe; `ctx.chain()` (PCR/IV/skew/
  max_pain); `ctx.iv_rank()` (ATM-IV percentile). `warmup_bars` param preloads
  indicator lookback in backtest + on paper/live (re)start.
Invariant #5 still holds: any Context change touches all four contexts
(backtest/paper/live/loader smoke) + prompt + example.

## Current gaps (see docs/ROADMAP.md for ordered milestones + prompts)
ALL ROADMAP MILESTONES DONE (M1–M8). M1 backfill / M2 WS feed / M3 chain poller
live-verified; M4 persistence, M6 walk-forward + Lab UI, M7 risk panel + view all
tested (M6/M7 browser-verified); M5 real margin (equity live-verified); M8 live
execution + kill switch built & gated, verified in DRY-RUN (no real orders).
Before real capital, ON THE VPS during market hours: (a) live tick→candle flow,
(b) chain poller under load, (c) real FNO margin + `scripts/calibrate_margin`,
(d) M8 real-order path incl. the OrderUpdate socket + fill reconciliation (BUILT
and unit-tested offline; the payload field names beyond orderNo/status are
unverified — see engines/order_updates.py) + kill_switch action strings. (MCX chain recording is LIVE since 2026-07-27 —
the dynamic resolver populates CRUDEOIL/GOLD ids; verified 100k+ rows/day.)
Flip live on only via /live/settings ON the VPS. (a) and (b) verified
2026-07-27/28. For any "is it healthy?" question run `scripts/diag.py` on the
VPS — one command: sha, service, token, per-table + per-underlying freshness,
scanner cadence/scores, warn/error tail, RED-flag verdict.
For "did we record the RIGHT MCX contract?" run `scripts/mcx_contract_audit.py`:
per-day bar coverage and chain-spot movement for an MCX name measured against
CRUDEOIL as the control (same session, feed and recorder, so it says what
'fully recorded' looked like that day — no absolute thresholds to re-tune).
Read-only by design: it names the suspect days and prints the DELETEs, but
removal is a deliberate decision, since MCX data can never be re-fetched.
`scripts/mcx_purge_dead_contract.py` does the removal when you've decided —
dry-run by default, imports the audit's suspect rule rather than restating it,
refuses to run while the service holds the DuckDB write lock, and writes every
affected row to Parquet and reads it back at the exact row count BEFORE
deleting, so the operation is reversible (the restore SQL is printed on
completion).
For "why is THIS chain not recording?" run `scripts/chain_probe.py` instead:
per-underlying freshness across BOTH chain tables (plus who else is keeping the
table warm), the self-heal ladder's timeline and every chain no-op printed IN
FULL (diag truncates at 70 chars, cutting off `raw_response=` exactly where it
matters), and with `--probe` it calls Dhan directly — expiry_list then
option_chain for the nearest expiry — printing the raw response. That last part
is the answer six incidents never captured. `--probe` uses its own client, so
its requests are OUTSIDE MarketHub's 3s gate and count against the same
per-account budget: it spaces them 5s and takes `--names` to probe only the
dark ones.
- Index-futures VOLUME companion (engines/feed.py + paper.py, gated behind the
  `index_futures_volume` setting, default OFF). VERIFIED against the installed
  dhanhq SDK: feed-mode/segment ints (Ticker15/Full21/IDX0/NSE_FNO2/BSE_FNO8),
  the FUTIDX instrument + `NAME-MonYYYY-FUT` symbol format, and the packet keys
  — the companion subscribes FULL (mode 21), NOT Quote, because only Full's
  single packet carries OI with LTP+volume (Quote's OI is a separate LTP-less
  packet _handle_packet drops). STILL VPS-pending during market hours (needs
  the registered static IP): that resolve_index_futures() returns the right live
  contract and that a Full subscription actually streams increasing volume+OI —
  run `venv/bin/python -m scripts.verify_index_futures`. Flip the setting on
  only after that passes.
- Scanner auto-trader now joins the main portfolio: `GET /portfolio/today`
  includes its allocated capital (`scanner_trade_capital`, default 5L) and
  P&L in `equity`/`growth`/totals whenever `scanner_trade` is on, and its
  open positions + today's fills appear in the unified `open_positions`/
  `trades_today` lists (PositionsView.jsx renders these directly now, no
  more per-strategy-only "By strategy" table). Fixed a real bug in the same
  pass: `ScannerTrader.step()` was overwriting daily_pnl's realized/fees for
  today with only the LAST cycle's incremental amount instead of the day's
  running total (`ScannerTrader._book_day` now reads-modifies-writes against
  the existing row). Also split `step()` into `manage()` (mark/exit only) +
  a new fast `run_position_mtm` loop (scanner.py, ~20s cadence, held-symbols-
  only, capped at `MAX_FAST_MTM_SYMBOLS`) so open scanner positions' P&L and
  stop/target checks don't lag up to `TIER2_INTERVAL` (5 min) behind real
  time. STILL VPS-pending during market hours: that the new loop's chain
  polling behaves under the real 3s rate gate alongside the existing pollers
  without starving them, and that `daily_pnl` accumulates correctly across
  a full live session with real order volume (only unit-tested offline so
  far, see tests/test_scanner_trader.py / test_scanner_tier2.py).
