#!/usr/bin/env bash
# Idempotent deploy/update for OptionsLab. Run ON THE VPS from the repo root:
#   bash deploy/deploy.sh
# Installs Python deps, builds the frontend, and restarts the service if present.
# Safe to re-run for every update.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
echo "==> OptionsLab deploy in $ROOT"

# 1) Python venv + backend deps ------------------------------------------------
if [ ! -d venv ]; then
  echo "==> creating venv"
  python3 -m venv venv
fi
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt
echo "==> python deps ok"

# 2) Frontend build (canonical UI is frontend/ -> app/static/) ------------------
if command -v npm >/dev/null 2>&1; then
  ( cd frontend && npm ci --no-audit --no-fund && npm run build )
  echo "==> frontend built to app/static/"
else
  echo "!! npm not found — install Node 18+ to build the dashboard" >&2
  exit 1
fi

# 3) Sanity: offline test suite (no creds/network needed) ----------------------
if [ "${SKIP_TESTS:-0}" != "1" ]; then
  venv/bin/pip install -q pytest
  venv/bin/python -m pytest tests/ -q || {
    echo "!! tests failed — aborting restart (set SKIP_TESTS=1 to bypass)" >&2; exit 1; }
fi

# 4) Restart the service if it's installed -------------------------------------
if [ -f /etc/systemd/system/optionslab.service ] \
    || systemctl list-unit-files 2>/dev/null | grep -q '^optionslab.service'; then
  echo "==> restarting optionslab service"
  sudo systemctl restart optionslab
  sleep 2
  # status needs no root — keeps the autopull sudoers grant restart-only
  systemctl --no-pager --lines=0 status optionslab | head -4
  curl -fsS http://localhost:8000/token/status >/dev/null && echo "==> app responding on :8000" \
    || echo "!! app not responding yet — check: journalctl -u optionslab -n 50"
else
  echo "==> service not installed yet. First time? see deploy/SETUP.md, then:"
  echo "    sudo cp deploy/optionslab.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now optionslab"
fi
echo "==> done"
