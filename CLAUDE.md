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
  mode), trades blotter, events log, settings, param overrides, and
  paper_state (M4: per-strategy live-session snapshot for restart recovery).
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
  Fill reconciliation (OrderUpdate WS) + real market-hours run are the next step.
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
- `app/data/store.py` — DuckDB schema (underlying_bars, option_bars with
  ATM-relative keys) + SyntheticStore fallback.
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

## Current gaps (see docs/ROADMAP.md for ordered milestones + prompts)
ALL ROADMAP MILESTONES DONE (M1–M8). M1 backfill / M2 WS feed / M3 chain poller
live-verified; M4 persistence, M6 walk-forward + Lab UI, M7 risk panel + view all
tested (M6/M7 browser-verified); M5 real margin (equity live-verified); M8 live
execution + kill switch built & gated, verified in DRY-RUN (no real orders).
Before real capital, ON THE VPS during market hours: (a) live tick→candle flow,
(b) chain poller under load, (c) real FNO margin + `scripts/calibrate_margin`,
(d) M8 real-order path incl. OrderUpdate fill reconciliation (not yet built) +
kill_switch action strings. Also pending: MCX chain recording needs MCX security
ids in dhan_client.UNDERLYINGS. Flip live on only via /live/settings ON the VPS.
