#!/usr/bin/env bash
# One-time installer for the auto-update loop. Run ON THE VPS (directly or via
# ssh) as a sudo-capable user:
#   curl -fsSL https://raw.githubusercontent.com/patilprem/Options-Lab/main/deploy/install_autopull.sh -o /tmp/install_autopull.sh
#   bash /tmp/install_autopull.sh
# Location-independent and self-bootstrapping: it first turns /opt/optionslab
# into a git checkout of origin/main (scp-era installs have no repo there), so
# it works even when no deploy/ files exist on the box yet. Databases, venv,
# and the real credentials unit file are untracked — never touched.
# After this, the VPS checks GitHub main every 5 minutes and redeploys itself
# (outside IST market hours). You never scp/ssh code up again — just push.
set -euo pipefail
ROOT="${OPTIONSLAB_ROOT:-/opt/optionslab}"
REPO_URL="${OPTIONSLAB_REPO:-https://github.com/patilprem/Options-Lab.git}"
RUN_AS="${OPTIONSLAB_USER:-optionslab}"

command -v git >/dev/null || sudo apt-get install -y git

# 1) make $ROOT a checkout of origin/main (bootstraps scp-era installs in place)
if [ ! -d "$ROOT/.git" ]; then
  echo "==> bootstrapping git repo in $ROOT"
  sudo -u "$RUN_AS" git -C "$ROOT" init -q
  sudo -u "$RUN_AS" git -C "$ROOT" remote add origin "$REPO_URL"
fi
sudo -u "$RUN_AS" git -C "$ROOT" fetch -q origin main
sudo -u "$RUN_AS" git -C "$ROOT" checkout -qf -B main origin/main
echo "==> repo at $(sudo -u "$RUN_AS" git -C "$ROOT" rev-parse --short HEAD)"

# 2) deploy.sh restarts the service as the service user; allow exactly that
# one command without a password (restart only — not a general systemctl grant)
echo "$RUN_AS ALL=(root) NOPASSWD: /usr/bin/systemctl restart optionslab" \
  | sudo tee /etc/sudoers.d/optionslab-restart >/dev/null
sudo chmod 440 /etc/sudoers.d/optionslab-restart

# 3) install + arm the timer
sudo cp "$ROOT/deploy/optionslab-autopull.service" \
        "$ROOT/deploy/optionslab-autopull.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now optionslab-autopull.timer
echo "==> timer installed:"
systemctl list-timers optionslab-autopull.timer --no-pager || true

# 4) first full deploy right now — the checkout above already fetched the
# code, so run deploy.sh directly (deps + UI build + tests + restart);
# autopull would see HEAD == origin/main and correctly no-op
sudo -u "$RUN_AS" bash "$ROOT/deploy/deploy.sh"
echo "==> done. Watch future runs with: journalctl -u optionslab-autopull -f"
