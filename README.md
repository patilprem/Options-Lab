# OptionsLab

Personal options strategy platform for Indian markets (NSE / BSE / MCX)
built on DhanHQ v2 APIs. Paste an LLM-generated strategy, validate it,
backtest it date-by-date, deploy it to a paper-trading engine, play/pause
it, and allocate capital per strategy.

**New here?** Read [`docs/OVERVIEW.md`](docs/OVERVIEW.md) first — it says
what we're trying to achieve, where the edge is meant to come from, what
would count as success, and the evidence rules the system runs on. This
README covers the mechanics.

## How "paste LLM code" works

The platform does not parse arbitrary code. Instead:

1. You give your LLM `prompts/strategy_prompt.md` + a plain-English
   description of the strategy.
2. The LLM returns a class that subclasses `Strategy` (see
   `app/core/contract.py`) — fixed hooks: `meta`, `on_bar`, `on_fill`,
   `on_day_end`... All market data and orders go through the `ctx` object.
3. You POST that code to `/strategies`. The loader
   (`app/core/loader.py`) AST-scans it (no os/network/eval), compiles it
   in a restricted namespace, and smoke-tests it on synthetic bars.
4. The exact same strategy object then runs in the backtest engine and
   the paper engine — only the `Context` behind it changes.

## Quickstart

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
# open http://127.0.0.1:8000/docs
```

With zero configuration the platform uses a synthetic market
(`SyntheticStore`) so you can exercise the full flow immediately:

1. `POST /strategies` with `{"name": "...", "code": <contents of
   examples/short_straddle_920.py>}` → returns id, state VALIDATED
2. `POST /strategies/{id}/backtest` `{"from_date":"2025-01-06",
   "to_date":"2025-01-17","capital":1000000}` → summary + daily P&L
3. `POST /strategies/{id}/allocate` `{"capital": 500000}`
4. `POST /strategies/{id}/deploy` → DEPLOYED_PAUSED
5. `POST /strategies/{id}/play` / `.../pause`
6. `GET  /strategies/{id}/performance` → day P&L + open positions

## Dashboard

`http://<server>:8000/` serves the UI — a React + Vite SPA in
`frontend/`, built to `app/static/` (`cd frontend && npm run build`):
status tape (IST market clock, token countdown, engine heartbeat),
portfolio summary (allocated / equity / growth / live count), strategy
cards with state badges, and a detail panel with paper metrics + equity
curve, a backtest runner (with a friendly "data unavailable — paper
trade it anyway" path), and the code view. Play / Pause / Stop / Deploy /
Allocate all live in the panel header.

## Daily token refresh (24-hour SEBI rule)

One-time setup on the DhanHQ portal: switch to API-key mode, create a
key with Redirect URL = `https://<your-server>/dhan/callback`, set up
TOTP, and register your server's static IP. Put `DHAN_CLIENT_ID`,
`DHAN_API_KEY`, `DHAN_API_SECRET`, `NTFY_TOPIC` in the environment.

Then every day at 08:30 IST, `token_manager.daily_refresh_loop` checks
whether the token survives past market close. If not, it builds a login
link and pushes it to your phone via ntfy.sh — you tap it, log in with
PIN/TOTP on Dhan's page, Dhan redirects to `/dhan/callback`, and the
server captures and stores the fresh token. ~20 seconds from your phone;
no computer needed. The dashboard's token chip shows hours remaining and
a manual Refresh button; `/token/manual` accepts a pasted token as a
last resort.

## Running unattended

Deploy on a small VPS (₹300–600/month) so strategies run without your
laptop: `deploy/optionslab.service` is a systemd unit with auto-restart.
Point the Dhan static-IP setting at the VPS IP. The paper engine acts
only during market hours; daily P&L rows persist to SQLite so the
dashboard shows performance history whenever you check in. Open paper
positions are snapshotted to `registry.paper_state` on every fill/close
and on a 60 s heartbeat, so a mid-session restart recovers them.

Deploys are pull-based: pushing to `main` IS the deploy
(`optionslab-autopull.timer` fetches every 5 min), and restarts are
deferred during IST market hours so a redeploy never drops the live feed
or chain recording. `[force-deploy]` in a commit message overrides.

## Wiring real DhanHQ data

1. Set `DHAN_CLIENT_ID` / `DHAN_ACCESS_TOKEN` in the environment, or let
   `app/core/token_manager.py` manage the token (see below). SEBI rules:
   access tokens last 24 h, and you must run from a static IP registered
   with Dhan.
2. Backfill history into DuckDB:
   `python -m app.data.dhan_client backfill NIFTY 2024-01-01 2025-06-30`
   - underlying candles: 90-day chunks (Dhan intraday historical, 5 yrs)
   - option candles: Dhan expired-options ("rollingoption") API —
     minute-level, ATM-relative strikes, 30 days/call, up to 5 yrs,
     NSE & BSE (MCX expired data: verify with Dhan; for commodities,
     start recording live chain snapshots now to build your own history).
3. Live data is wired: `app/engines/feed.py` drives the dhanhq
   `MarketFeed` WebSocket (ticks → candle builder) and `MarketHub` runs
   the chain poller (Option Chain API, max 1 unique request per 3 s) for
   greeks/IV and bid/ask. `OPTIONSLAB_SYNTHETIC=1` (or absent creds)
   falls back to the synthetic replay.

## Play / pause semantics (deliberate design choice)

- **Pause** blocks NEW entries but keeps managing exits, so stop-losses
  still protect open positions. Set `square_off_on_pause=true` on
  `/allocate` if you prefer flatten-on-pause.
- **Stop** squares off everything and unloads the strategy.
- A strategy that throws inside `on_bar` is auto-paused, not killed.

## Capital allocation

`allocated_capital` is virtual money per strategy instance. Both engines
reject entries whose estimated margin exceeds available capital. Paper
and live use `app/engines/margin.py:real_margin`, which sums Dhan's
per-leg `margin_calculator` across the structure; backtests use the
cheaper `fills.py:estimate_margin` scaled by a per-underlying calibration
factor (setting `margin_factor:<underlying>`).

## Costs model

`app/engines/fills.py` charges brokerage/STT/exchange txn/GST/SEBI/stamp
per order and fills at bid/ask live or close±synthetic-spread in
backtests (spread widens with distance from ATM). Verify rates against a
real Dhan contract note before trusting absolute P&L.

## What's NOT here yet

Built since this README was first written: real Dhan WS feed,
walk-forward + Monte Carlo, the React frontend (`frontend/` → built into
`app/static/`), risk panel, real margin, the scanner auto-trader, the
insight + adaptation engines, and gated live order routing with Dhan's
kill switch.

Still outstanding — and all of these gate real capital:

- **Broker position reconciliation** — the app's positions are not yet
  checked against Dhan's.
- **Fill reconciliation** via the OrderUpdate WebSocket — fills are
  assumed, not confirmed.
- **Real FNO SPAN margin verification** during market hours (equity
  margin is live-verified) + `scripts/calibrate_margin`.
- **MCX chain recording** — needs MCX security ids in
  `dhan_client.UNDERLYINGS`.

Live execution is built and dry-run verified; no real order has been
sent. See `docs/OVERVIEW.md` §7 for the five gates that must all be open
before one can be.

## Layout

```
app/
  core/contract.py    the Strategy/Context interface (the heart)
  core/loader.py      AST validation + restricted load + smoke test
  core/registry.py    lifecycle state machine, allocation, SQLite
  engines/fills.py    fills, Indian option charges, margin estimate
  engines/margin.py   real per-leg margin via Dhan, with fallback
  engines/backtest.py event-driven backtester, date-by-date P&L
  engines/walkforward.py  K-fold walk-forward + adaptive_search
  engines/paper.py    live paper engine, MarketHub, chain poller
  engines/feed.py     dhanhq MarketFeed driver + tick→candle builder
  engines/chain.py    option-chain normalizer (ATM-relative quotes)
  engines/indicators.py   the indicator toolbox strategies use
  engines/risk.py     portfolio + per-strategy risk caps
  engines/live.py     gated live execution (default: dry run)
  engines/scanner_trader.py   the F&O scanner auto-trader
  engines/*_insights.py, adaptation.py, strategy_adapt.py
                      trade-log analytics → gated param proposals
  data/store.py       DuckDB store + synthetic fallback
  data/dhan_client.py Dhan downloaders + backfill CLI
  api/strategies.py   REST endpoints
  main.py             FastAPI app
frontend/             React + Vite SPA, builds into app/static/
docs/OVERVIEW.md      what we're trying to achieve, and the rules
prompts/strategy_prompt.md  give this to your LLM
examples/short_straddle_920.py  known-good pasteable strategy
```
