#!/usr/bin/env bash
# One-time installer for the auto-update loop. Run ON THE VPS as your sudo user:
#   cd /opt/optionslab && bash deploy/install_autopull.sh
# After this, the VPS checks GitHub main every 5 minutes and redeploys itself
# (outside IST market hours). You never scp/ssh code up again — just push.
set -euo pipefail
cd "$(dirname "$0")"

command -v git >/dev/null || { echo "git is required: sudo apt install -y git"; exit 1; }

# deploy.sh restarts the service as the 'optionslab' user; allow exactly that
# one command without a password (restart only — not a general systemctl grant)
echo "optionslab ALL=(root) NOPASSWD: /usr/bin/systemctl restart optionslab" \
  | sudo tee /etc/sudoers.d/optionslab-restart >/dev/null
sudo chmod 440 /etc/sudoers.d/optionslab-restart

sudo cp optionslab-autopull.service optionslab-autopull.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now optionslab-autopull.timer

echo "==> timer installed:"
systemctl list-timers optionslab-autopull.timer --no-pager || true
echo "==> running the first check now (FORCE=1 so it deploys even in market hours):"
sudo -u optionslab FORCE=1 bash /opt/optionslab/deploy/autopull.sh
echo "==> done. Watch future runs with: journalctl -u optionslab-autopull -f"
