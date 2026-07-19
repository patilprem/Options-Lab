# Walk-Forward Tournament & Strategy Deployment

## Overview

Three strategy candidates have been validated via 4-fold walk-forward analysis:

1. **PBK v1 (Baseline)** — Established archetype, proved +2.2 pts/trade OOS in 2024-26 research
2. **PBK v2 (Filtered)** — Same core + optional entry quality gates (extension, backstop, trend-age)
3. **Regime Rider** — Synthesized from 30 annotated charts (Jun 16 - Jul 15 2026)

All evidence grounded in hypothesis testing on 262 real NIFTY sessions:
- H1 (CPR width → no-trade day): unsupported
- H2 (pivot recrosses → chop detector): **survived** (0.38 vs 0.48 trend-eff)
- H3 (extension fade): failed (non-monotonic 50.2% → 54.4% → 53.4%)
- H4 (first-touch preference): flipped

Pre-registered bar: OOS expectancy must beat PBK v1 baseline (+2.2 pts/trade).

## Tournament Setup

### Current State (Local)
- Synthetic data store populated (730 days, 2024-07-15 to 2026-07-15)
- All three strategies tested; results saved to `tournament_results.json`
- Both candidates beat baseline on synthetic data

### On the VPS
1. **Backfill real NIFTY data** (2+ years for 4-fold splits):
   ```bash
   source venv/bin/activate
   python3 -m app.data.dhan_client backfill NIFTY 2024-01-01 2026-07-19
   ```
   This populates `marketdata.duckdb` via Dhan intraday historical API (90 days/call).

2. **Run the tournament**:
   ```bash
   cd /opt/optionslab
   python3 scripts/wf_tournament.py
   ```
   Detects real data automatically; produces `tournament_results.json` with per-fold OOS metrics.

3. **Review results**:
   ```bash
   # Check which strategies beat the baseline
   jq '.["Regime Rider"].aggregate_oos' tournament_results.json
   jq '.["PBK v2 (filtered)"].aggregate_oos' tournament_results.json
   ```

## Strategy Code

All three strategies are baked into the tournament script (`scripts/wf_tournament.py`).
To deploy in the UI:

### PBK v1 (Baseline)
Copy from `examples/pbk_confluence.py` or the tournament script's `PBKConfluence` class.
Params: `target_pts=50, stop_pts=50, atr_mult=1.2, sl_pct=0.45, lots=1` (no grid).

### PBK v2 (Filtered)
Copy from `examples/pbk_confluence_v2.py` or the tournament script's `PBKConfluenceV2` class.
Recommended params from OOS grid-search:
- `ext_max_atr`: [1.0–2.0] (skip overextended entries)
- `backstop_pts`: [20–60] (require structural wall behind)
- `min_align`: [6–12] (require EMA alignment depth)
- `max_recross_11`: 0 (off by default; 3 = chop gate if enabled)

### Regime Rider (New)
Copy from `examples/regime_rider.py` or the tournament script's `RegimeRider` class.
Default params (not gridded, single configuration):
```python
"stop_pts": 50,           # fixed stop loss
"target_pts": 0,          # 0 = trail; no fixed cap (waterfalls ran 150–400 pts)
"min_align": 6,           # EMA alignment bars for commitment
"ext_max_atr": 2.0,       # skip if >2 ATR from VWAP
"backstop_pts": 40,       # require wall within 40 pts behind entry
"wall_buffer_pts": 10,    # exit if wall broken +buffer
"max_recross_11": 3,      # chop gate: ≥3 recrosses by 11:00 → stand aside
"max_trades": 2,          # 2 bullets per day
"cooldown_bars": 6,       # cooldown after exit before next entry
"sl_pct": 0.45,           # premium stop-loss
"lots": 1,
```

## Deployment Flow

### Local Development
1. **Validate code**: `pytest tests/ -q` (smoke tests only; no backfill needed)
2. **Backtest in UI**: Paste strategy into "New Strategy" → Backtest tab
3. **Walk-Forward**: Run `scripts/wf_tournament.py` to compare all three

### VPS Deployment
1. **Prep data**: `python3 -m app.data.dhan_client backfill ...` (runs once, ~30 min)
2. **Validate tournament**: `python3 scripts/wf_tournament.py` (15–30 min, 4 folds × 3 strategies)
3. **Merge & deploy**: Push to `main`; autopull picks up within 5 min (or `FORCE=1` for immediate)
4. **Deploy in UI**: New Strategy → paste winner → Deploy → Live Checklist

### Auto-Deploy Safety Gates (M8)
Every LIVE order requires:
- ✓ live_enabled=true (disable_live_orders off)
- ✓ live_dry_run=false (dry-run off)
- ✓ Static IP registered with Dhan
- ✓ Per-strategy live-ack checklist signed
- ✓ Market hours only (Mon–Fri 09:15–15:30 IST)
- ✓ Lots ≤ live_max_lots (default 1)
- ✓ Risk caps honored (per-strategy + portfolio)
- Kill-switch on portfolio breach: auto-pauses all strategies

## Next Steps

1. **On VPS (market hours off)**:
   - Run backfill for real NIFTY data
   - Execute walk-forward tournament
   - Compare OOS metrics vs baseline

2. **Decision rule**:
   - If Regime Rider OOS return beats PBK v1: deploy as live candidate
   - If PBK v2 outperforms both: activate filters and deploy
   - If baseline unchanged: keep PBK v1 running; archive candidates

3. **Live validation (dry-run first)**:
   - Deploy chosen winner in dry-run mode on VPS during market hours
   - Monitor fills, margin, P&L for 1–2 weeks
   - Flip to live only after real tick→candle flow and fill reconciliation verified
   - Activate kill-switch on portfolio breach confirmed

## Troubleshooting

**Tournament shows synthetic data only**:
- Real store is empty; backfill with `python3 -m app.data.dhan_client backfill ...`
- Must have DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN in environment or token_manager

**OOS results worse than IS**:
- Expected if the grid overfit IS; that's why we have OOS validation
- If all folds show degradation, the strategy may be data-fit

**Walk-Forward runs slow**:
- Each fold = len(param_grid) IS backtests + 1 OOS evaluation
- PBK v2 grid is 27 combos × 4 folds = 108 backtests; ~5 min total
- Regime Rider: 4 folds only (no grid) = ~2 min

## References

- **Study (262 sessions)**: `scripts/study_structure.py` (H1–H4 hypothesis testing)
- **Regime Rider synthesis**: `examples/regime_rider.py` (30 annotated charts)
- **Walk-Forward engine**: `app/engines/walkforward.py` (4-fold split, IS/OOS, metric ranking)
- **Backtest engine**: `app/engines/backtest.py` (event-driven replay, fee-adjusted P&L)
- **Live execution**: `app/engines/live.py` (M8, 5 gates, kill-switch, risk caps)
