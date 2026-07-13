# Deploying OptionsLab — minimal cost, personal use

The app is one Python process + two local database files. It needs:
a machine that stays on during market hours, a stable public egress IP
(for Dhan's static-IP rule), and HTTPS reachable from *your phone only*
(for the token-refresh callback and the dashboard). That's it.

## Recommended stack (₹0–₹450/month for the server)

| Piece | Choice | Cost |
|---|---|---|
| Server | Oracle Cloud "Always Free" ARM VM (Mumbai) — 4 cores / 24 GB, static public IP | ₹0 |
| ...or fallback | AWS Lightsail (Mumbai) or DigitalOcean (Bangalore), smallest plan | ~₹350–450/mo |
| Private access + HTTPS | Tailscale personal plan | ₹0 |
| Push notifications | ntfy.sh public topic | ₹0 |
| Domain | none needed (Tailscale gives you `*.ts.net` with valid certs) | ₹0 |
| Dhan Data API | subscription (unavoidable) | ₹499 + GST /mo |

Oracle's free tier is genuinely free forever but signup can be finicky
and Mumbai ARM capacity is sometimes full — retry or pick the paid
fallback. Any 1 GB RAM box runs the app fine; 2 GB+ is comfortable for
big DuckDB backfills.

## Why Tailscale instead of a public website

The dashboard controls your trading. Exposing it to the public internet
means building auth, rate limiting, and fail2ban — or getting popped.
Tailscale puts the server and your phone on a private network:

- Dashboard reachable from anywhere *your* devices are, nothing else.
- Free HTTPS certificate on a `https://optionslab.<tailnet>.ts.net` URL.
- The Dhan login redirect works because the redirect happens **in your
  phone's browser**, and your phone is on the tailnet — so even the
  token callback never needs public exposure.
- Zero cost, zero auth code to write, zero attack surface.

## Setup (roughly 30 minutes)

```bash
# 1. On the fresh Ubuntu server
sudo apt update && sudo apt install -y python3-venv unzip
sudo useradd -m optionslab
sudo mkdir -p /opt/optionslab && sudo chown optionslab /opt/optionslab

# 2. Copy the project up and install
#    (Node 18+ is needed to build the dashboard)
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo bash - && sudo apt-get install -y nodejs
scp optionslab.zip <server>:/tmp/ && ssh <server>
sudo -u optionslab bash -c '
  cd /opt && unzip /tmp/optionslab.zip && cd optionslab
  bash deploy/deploy.sh'     # venv + pip + build frontend/ + run tests (idempotent)

# 3. Tailscale (server side)
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up                      # log in once
sudo tailscale serve --bg 8000         # HTTPS :443 -> app :8000
tailscale status                       # note your https://….ts.net name
# install the Tailscale app on your phone and log in with the same account

# 4. Configure + start the service
sudo cp /opt/optionslab/deploy/optionslab.service /etc/systemd/system/
sudo nano /etc/systemd/system/optionslab.service   # fill the env vars:
#   DHAN_CLIENT_ID / DHAN_API_KEY / DHAN_API_SECRET
#   NTFY_TOPIC=optionslab-<something-random>
#   BASE_URL=https://<your-name>.ts.net
sudo systemctl daemon-reload
sudo systemctl enable --now optionslab
```

Then, one-time on the DhanHQ portal (API-key mode):
- Redirect URL: `https://<your-name>.ts.net/dhan/callback`
- Set up TOTP (makes the daily phone-tap login fast)
- Static IP setting: the server's public IP
  (`curl ifconfig.me` from the server; on Oracle/Lightsail it's static)

And on your phone: install the **ntfy** app, subscribe to your
`NTFY_TOPIC`. The 08:30 IST token reminder and future alerts land there.

## Ongoing costs, worst case

- Server: ₹0 (Oracle) or ~₹400 (Lightsail/DO)
- Dhan Data API: ₹499 + GST
- Everything else: ₹0

## Operational notes

- **Updates are automatic** once you run `bash deploy/install_autopull.sh`
  (one time, as your sudo user): a systemd timer checks GitHub `main` every
  5 minutes and redeploys via `deploy.sh` (deps + UI build + offline tests +
  restart). Restarts are DEFERRED during IST market hours so a deploy never
  bounces the live feed; force one with
  `sudo -u optionslab FORCE=1 bash /opt/optionslab/deploy/autopull.sh`.
  Watch runs: `journalctl -u optionslab-autopull -f`. Manual fallback still
  works: `git pull && bash deploy/deploy.sh` from `/opt/optionslab`.
- **Backups:** both databases are single files. A nightly cron
  `cp optionslab.db marketdata.duckdb /home/optionslab/backup/ && keep last 7`
  is enough; copy them off-box weekly if you're paranoid. `optionslab.db`
  (SQLite) holds strategies, the trade blotter, and daily P&L — back this up.

## Guardrails — read before running data jobs (learned the hard way)

- **One writer for `marketdata.duckdb`.** DuckDB allows a single writer per
  file. **Do NOT run a command-line backfill while the app server is up** —
  they collide, and the server silently falls back to the *synthetic* (fake)
  market. Use the in-app **Data → Pull history** button instead: it backfills
  *inside* the running server (shared connection), shows live progress, and
  resumes exactly if interrupted (a SQLite chunk ledger tracks completed
  chunks). If you must use the CLI, `sudo systemctl stop optionslab` first.
- **Backfills default to 2 years** of the strategy's underlying, ATM±2, 5-min.
  The expired-options endpoint is slow (~1 min/chunk; ~3–4 h for 2 years).
  A run survives a crash/shutdown: re-running resumes from the ledger.
- **Synthetic fallback is safe by design.** If real data is unavailable at
  startup (DB locked, or dev mode), the app will NOT auto-resume paper
  strategies on fake prices — it logs a warning and waits. Restart once the
  backfill is done and real data is available.
- **Never scale to >1 uvicorn worker** (see the systemd unit) — the engine
  state is in-memory; a second worker double-trades.
- If you ever *must* expose it publicly instead of Tailscale, put Caddy in
  front with basic-auth and a free DuckDNS domain — but the tailnet route is
  safer and simpler.

## Going live with real orders (M8 — do this last)

Live trading is OFF by default and gated. On the VPS, during market hours,
and only after paper-trading confidence:
1. Confirm the VPS's public IP is registered under Dhan's **Static IP** setting.
2. `POST /live/settings {"enabled": true}` — still dry-run (orders logged, not sent).
3. Watch a full session in **dry-run**, confirm the order previews are correct.
4. Only then `POST /live/settings {"dry_run": false}` and acknowledge the
   per-strategy live checklist in the dashboard. The kill switch and daily-loss
   caps (Risk tab) are enforced every bar.
