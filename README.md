# OptionsLab

Personal options strategy platform for Indian markets (NSE / BSE / MCX)
built on DhanHQ v2 APIs. Paste an LLM-generated strategy, validate it,
backtest it date-by-date, deploy it to a paper-trading engine, play/pause
it, and allocate capital per strategy.

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

`http://<server>:8000/` serves the UI (`app/static/index.html`):
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
dashboard shows performance history whenever you check in. Known gap:
open paper positions are held in memory, so a mid-session restart loses
them — persist `PaperContext` positions if that matters to you.

## Wiring real DhanHQ data

1. Put your client id + access token in `app/data/dhan_client.py`.
   (SEBI rules: access tokens last 24 h — schedule a daily refresh,
   and run from a static IP registered with Dhan.)
2. Backfill history into DuckDB:
   `python -m app.data.dhan_client backfill NIFTY 2024-01-01 2025-06-30`
   - underlying candles: 90-day chunks (Dhan intraday historical, 5 yrs)
   - option candles: Dhan expired-options ("rollingoption") API —
     minute-level, ATM-relative strikes, 30 days/call, up to 5 yrs,
     NSE & BSE (MCX expired data: verify with Dhan; for commodities,
     start recording live chain snapshots now to build your own history).
3. Replace `MarketHub.run_synthetic` with a driver that consumes the
   dhanhq `MarketFeed` WebSocket (ticks → candle builder) and a chain
   poller (Option Chain API, max 1 unique request per 3 s) for greeks/IV
   and bid/ask.

## Play / pause semantics (deliberate design choice)

- **Pause** blocks NEW entries but keeps managing exits, so stop-losses
  still protect open positions. Set `square_off_on_pause=true` on
  `/allocate` if you prefer flatten-on-pause.
- **Stop** squares off everything and unloads the strategy.
- A strategy that throws inside `on_bar` is auto-paused, not killed.

## Capital allocation

`allocated_capital` is virtual money per strategy instance. Both engines
reject entries whose estimated margin exceeds available capital.
`app/engines/fills.py:estimate_margin` is a rough SPAN stand-in — in
paper/live mode swap in Dhan's multi-leg margin calculator API (hedge
benefit included) and calibrate the backtest approximation from it.

## Costs model

`app/engines/fills.py` charges brokerage/STT/exchange txn/GST/SEBI/stamp
per order and fills at bid/ask live or close±synthetic-spread in
backtests (spread widens with distance from ATM). Verify rates against a
real Dhan contract note before trusting absolute P&L.

## What's intentionally NOT here yet

- Real Dhan WS binary parsing (use the official `dhanhq` pip package's
  MarketFeed)
- Walk-forward / Monte Carlo wrappers around `run_backtest` (both are
  thin loops over it — next step)
- Live order routing (add an ExecutionContext later; keep the kill-switch
  wired to Dhan's killswitch + P&L-based exit APIs)
- A frontend — the REST API is UI-ready (list, play/pause buttons,
  allocate dialog, equity charts from `daily` arrays)

## Layout

```
app/
  core/contract.py    the Strategy/Context interface (the heart)
  core/loader.py      AST validation + restricted load + smoke test
  core/registry.py    lifecycle state machine, allocation, SQLite
  engines/fills.py    fills, Indian option charges, margin estimate
  engines/backtest.py event-driven backtester, date-by-date P&L
  engines/paper.py    live paper engine, MarketHub, play/pause
  data/store.py       DuckDB store + synthetic fallback
  data/dhan_client.py Dhan downloaders + backfill CLI
  api/strategies.py   REST endpoints
  main.py             FastAPI app
prompts/strategy_prompt.md  give this to your LLM
examples/short_straddle_920.py  known-good pasteable strategy
```
