# Roadmap — ordered milestones with Claude Code prompts

Work these top to bottom. Each is sized for one focused Claude Code
session. Paste the prompt, review the plan it proposes, then let it work.

## M1 — Verify the Dhan backfill against the real API
> I have a DhanHQ data subscription. My credentials are in env vars.
> Run app/data/dhan_client.py backfill for NIFTY for the last 30 days,
> inspect the actual JSON the intraday and rollingoption endpoints
> return, and fix _upsert_option_rows / the intraday parsing to match
> the real response shapes. Then verify /data/coverage shows the range
> and run a real backtest of examples/short_straddle_920.py over it.
> Add a small integration test that replays a saved sample response.

## M2 — Real live feed
> Replace MarketHub.run_synthetic with a real driver using the dhanhq
> pip package's MarketFeed (binary WebSocket): subscribe to the spot/
> index of each deployed strategy's underlying, build candles of the
> strategy's timeframe from ticks, and emit ("bar", underlying, Bar)
> into the existing subscriber queues. Add reconnect with backoff, log
> feed connect/disconnect to registry.record_event, and keep the
> synthetic driver behind an OPTIONSLAB_SYNTHETIC=1 env flag for dev.

## M3 — Option chain poller (quotes for fills + greeks)
> Add a chain poller to MarketHub: every 3+ seconds per underlying
> (Dhan rate limit: 1 unique request / 3 s), fetch the option chain and
> cache OptionQuote objects (ltp, bid, ask, iv, oi, greeks) keyed the
> same way store.option_close resolves LegSpecs. Make PaperContext use
> this cache via hub.quote so paper fills cross real bid/ask. Also make
> the MCX recorder in app/main.py persist these snapshots into
> option_bars when mcx_recording is on.

## M4 — Paper position persistence
> Persist PaperContext open positions to SQLite on every fill/close and
> on a 60s heartbeat; on startup, PaperRunner should restore open
> positions and margin for strategies in RUNNING/DEPLOYED_PAUSED states
> and log a 'recovered N positions' event. Add a test that simulates a
> restart mid-session.

## M5 — Real margin
> Integrate Dhan's multi-leg margin calculator API behind a function
> with the same signature as fills.estimate_margin; use it in the paper
> engine (with caching + fallback to the estimate on API failure), and
> add a calibration script that compares estimates vs API for common
> structures and writes a per-underlying correction factor used by
> backtests.

## M6 — Walk-forward in the Lab
> Add a walk-forward endpoint and Lab UI: split a date range into K
> folds; for each fold, grid-search over user-selected param ranges on
> the in-sample window (reuse run_backtest with param overrides), then
> evaluate the best params on the out-of-sample window; report per-fold
> OOS results and the aggregate OOS equity curve. Cap total runs and
> stream progress via the events log.

## M7 — Risk panel (mandatory before live)
> Add a Risk view: portfolio max-daily-loss (auto-pause all strategies
> when breached, with an event), per-strategy daily loss caps, exposure
> grouped by underlying and expiry from open positions, and margin
> utilization vs total allocated. Wire the checks into PaperRunner so
> they run every bar.

## M8 — Live execution adapter (last, behind the kill switch)
> Design first, then implement: a LiveContext mirroring PaperContext
> that routes enter/exit through Dhan's order APIs (super orders for
> SL), writes to the LIVE ledgers, arms Dhan's kill-switch and P&L-based
> exit, and honors the Risk panel caps. Deployment must go through the
> live-modal checklist. Keep lot size minimal by default. Do not touch
> paper code paths.

## Working style that fits this repo
- One milestone per session; commit after each green step.
- Ask Claude Code to write/extend tests before refactors of engines.
- After any Context interface change, remind it of invariant #5 in
  CLAUDE.md (both engines + smoke context + prompt template + example).
- Real-API work (M1/M2/M3/M5) is best run ON the VPS where the static
  IP and token live: install Claude Code there and work over SSH.
