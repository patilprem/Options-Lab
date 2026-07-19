#!/usr/bin/env bash
# Run the walk-forward tournament on the VPS with real NIFTY data.
# Usage: bash deploy/run_tournament.sh [--backfill] [--force-backfill]
#
# --backfill          Run backfill before tournament (default if DB is empty)
# --force-backfill    Force backfill even if DB already has data

set -euo pipefail

ROOT="${OPTIONSLAB_ROOT:-/opt/optionslab}"
cd "$ROOT"

source venv/bin/activate

BACKFILL=0
FORCE_BACKFILL=0

for arg in "$@"; do
    case "$arg" in
        --backfill)        BACKFILL=1 ;;
        --force-backfill)  BACKFILL=1; FORCE_BACKFILL=1 ;;
    esac
done

# Check if DB has data
echo "[tournament] Checking data store..."
HAS_DATA=$( python3 << 'EOF'
try:
    from app.data.store import DataStore
    store = DataStore()
    result = store._q1("SELECT COUNT(*) FROM underlying_bars WHERE underlying = 'NIFTY'")
    print(1 if (result and result[0] > 0) else 0)
except:
    print(0)
EOF
)

if [ "$HAS_DATA" -eq 0 ]; then
    echo "[tournament] No NIFTY data found; backfill required"
    BACKFILL=1
fi

# Backfill if needed
if [ "$BACKFILL" -eq 1 ]; then
    if [ "$FORCE_BACKFILL" -eq 0 ] && [ "$HAS_DATA" -eq 1 ]; then
        echo "[tournament] Skipping backfill (already has data; use --force-backfill to override)"
    else
        echo "[tournament] Backfilling NIFTY 2024-07-19 to 2026-07-19 (2y, ~90 days/call)..."
        # Requires DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN in environment or token_manager
        python3 -m app.data.dhan_client backfill NIFTY 2024-07-19 2026-07-19 || {
            echo "[tournament] ✗ Backfill failed (check Dhan credentials and token_manager)"
            exit 1
        }
        echo "[tournament] ✓ Backfill complete"
    fi
else
    echo "[tournament] Using existing data"
fi

# Run tournament
echo "[tournament] Starting 4-fold walk-forward tournament..."
echo "[tournament] PBK v1 (baseline) → PBK v2 (filtered grid) → Regime Rider (new)"
echo "[tournament] Pre-registered bar: OOS must beat +2.2 pts/trade"
echo ""

python3 scripts/wf_tournament.py

# Parse and display results
echo ""
echo "[tournament] ✓ Tournament complete"
echo "[tournament] Results saved to: tournament_results.json"
echo ""
echo "[tournament] Next step: review OOS metrics and decide deployment"
python3 << 'EOF'
import json
from pathlib import Path

results = json.loads(Path("tournament_results.json").read_text())
baseline_oos = results.get("PBK v1 (baseline)", {}).get("aggregate_oos", {})
baseline_return = baseline_oos.get("return_pct", 0)

print("PBK v1 (baseline) OOS return: {:.2f}%".format(baseline_return))
print()

for name in ["PBK v2 (filtered)", "Regime Rider"]:
    res = results.get(name, {})
    if res.get("status") == "error":
        print("{}: FAILED ({})".format(name, res.get("message")))
    else:
        oos = res.get("aggregate_oos", {})
        oos_return = oos.get("return_pct", 0)
        delta = oos_return - baseline_return
        if delta > 0:
            print("{}: ✓ PASS (+{:.2f}%)".format(name, delta))
        else:
            print("{}: ✗ FAIL ({:.2f}%)".format(name, delta))
EOF

exit 0
