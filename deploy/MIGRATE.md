# Migrating OptionsLab to the ARM box (micro → Ampere A1)

Moving the live instance from the tiny `E2.1.Micro` (1 core / 1 GB) to the
`optionslab-arm` Ampere A1 VM (4 cores / 24 GB) — the server `deploy/SETUP.md`
was always meant to run on. Both are Oracle "Always Free", so this is a free
upgrade, not a cost change.

The app is just **one Python process + two local database files**, and both DB
files are architecture-independent, so the move is mostly: stand the app up on
ARM, copy two files across, re-point Dhan/Tailscale at the new IP, cut over.

> **Do the whole cutover OFF-HOURS** (weekend or after MCX close, ~23:35 IST).
> Never run *both* boxes recording against the same Dhan token during market
> hours — duplicate WS connections and a split-brain `marketdata.duckdb`.

Throughout: `OLD` = the micro (`130.210.25.248`), `NEW` = the ARM box
(`137.23.55.67`, private `10.0.0.45`). Replace paths/user with your real ones
(`/opt/optionslab`, service user `optionslab`).

---

## 0. The one thing that silently breaks the feed: the IP

The ARM box has a **different public IP**. Per the Dhan static-IP rule
(`deploy/SETUP.md` → "Going live"), the live-order path and the token callback
key off it. Before cutover:

1. **Reserve the ARM public IP** in OCI so it can't change:
   *Networking → reserved public IPs*, or on the instance's VNIC assign a
   **reserved** (not ephemeral) public IP. Confirm from the box:
   ```bash
   curl -s ifconfig.me        # must equal the reserved IP, stable across reboots
   ```
2. **Whitelist that IP with Dhan** (portal → Static IP setting) and update the
   **Redirect URL** to the NEW Tailscale name's `/dhan/callback` (step 3).
   Until this is done, keep `live` OFF — paper/recording still work, but real
   orders would be rejected.

---

## 1. Provision the app on the ARM box

SSH to NEW and run the standard `SETUP.md` flow — it's identical on ARM (all
deps ship aarch64 wheels; Vite/Node build fine on arm64):

```bash
sudo apt update && sudo apt install -y python3-venv unzip git
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo bash - && sudo apt-get install -y nodejs
sudo useradd -m optionslab 2>/dev/null || true
sudo mkdir -p /opt/optionslab && sudo chown optionslab /opt/optionslab

# clone straight from main (no scp of code — autopull will keep it current)
sudo -u optionslab git clone https://github.com/patilprem/Options-Lab.git /opt/optionslab
sudo -u optionslab bash -c 'cd /opt/optionslab && bash deploy/deploy.sh'
#   ^ venv + pip + frontend build + offline tests. On 24 GB this is quick and
#     the Vite build won't OOM the way it can on the 1 GB micro.
```

Do **not** `systemctl enable --now optionslab` yet — start it only after the
data is copied (step 4), so it never boots on an empty/synthetic store.

## 2. Tailscale on the ARM box

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale serve --bg 8000          # HTTPS :443 -> app :8000
tailscale status                        # note the new https://<name>.ts.net
```

Use the NEW `*.ts.net` name in the service unit's `BASE_URL` and in Dhan's
Redirect URL (step 0.2).

## 3. Service unit + secrets (never in git)

```bash
sudo cp /opt/optionslab/deploy/optionslab.service /etc/systemd/system/
sudo nano /etc/systemd/system/optionslab.service     # fill the real values:
#   DHAN_CLIENT_ID / DHAN_API_KEY / DHAN_API_SECRET   (copy from OLD's unit)
#   NTFY_TOPIC=<same topic as OLD, so your phone keeps working>
#   BASE_URL=https://<new-name>.ts.net
sudo systemctl daemon-reload
```
Copy the exact env block from OLD:
`sudo cat /etc/systemd/system/optionslab.service` on the micro.

## 4. Copy the two databases (the actual data move)

Both files are portable across x86↔ARM as-is — no export/import.

```bash
# ON OLD — stop the writer first so the files are consistent, then fingerprint:
sudo systemctl stop optionslab optionslab-autopull.timer
/opt/optionslab/venv/bin/python /opt/optionslab/scripts/migrate_verify.py --save /tmp/old.json

# copy both DBs + the fingerprint OLD -> NEW (via the tailnet or scp)
scp /opt/optionslab/optionslab.db      optionslab@<NEW-ip-or-tsname>:/opt/optionslab/
scp /opt/optionslab/marketdata.duckdb  optionslab@<NEW-ip-or-tsname>:/opt/optionslab/
scp /tmp/old.json                      optionslab@<NEW-ip-or-tsname>:/tmp/

# ON NEW — confirm the copy matches byte-for-byte on the metrics that matter:
sudo chown optionslab /opt/optionslab/optionslab.db /opt/optionslab/marketdata.duckdb
sudo -u optionslab /opt/optionslab/venv/bin/python \
     /opt/optionslab/scripts/migrate_verify.py --compare /tmp/old.json
#   -> exits 0 and prints "MATCH" when every count/date range lines up.
```

If `--compare` reports a MISMATCH, do **not** retire OLD — recopy the offending
file.

## 5. Cut over

```bash
# NEW: bring the app up on the copied data
sudo systemctl enable --now optionslab
curl -fsS http://localhost:8000/token/status && echo " app up"
# then in the dashboard / logs, confirm within a minute or two:
journalctl -u optionslab -f
#   - feed connects (live tick during a session, or the MCX evening canary)
#   - chain poller refreshes (no "chain poll error" storm)
#   - /token/status healthy; tap the ntfy login link if a token refresh is due

# NEW: arm auto-updates (one time) so pushes to main redeploy here from now on
bash /opt/optionslab/deploy/install_autopull.sh

# Point your phone's dashboard bookmark at the NEW *.ts.net name.
```

Only once NEW has run clean through a session (feed + chain + a paper day
persisted):

```bash
# OLD: retire it so it can never double-record or double-trade
sudo systemctl disable --now optionslab optionslab-autopull.timer
```
Leave the OLD box **stopped but not terminated** for a few days as a rollback.

## 6. Rollback (if NEW misbehaves)

Nothing destructive happened to OLD — its DBs are intact and its service is
only disabled. To fall back: stop NEW's service, re-enable OLD's
(`sudo systemctl enable --now optionslab optionslab-autopull.timer`), and
re-whitelist OLD's IP with Dhan. Because NEW may have recorded newer rows,
copy NEW's DBs back to OLD first (repeat step 4 in reverse) if you want to keep
them.

---

## Checklist

- [ ] ARM public IP reserved (stable across reboot) and whitelisted with Dhan
- [ ] Dhan Redirect URL updated to the NEW `*.ts.net/dhan/callback`
- [ ] `deploy.sh` green on ARM (deps + build + tests)
- [ ] Tailscale up; `tailscale serve` on :8000; new `*.ts.net` noted
- [ ] Service unit env copied from OLD (creds + NTFY_TOPIC + new BASE_URL)
- [ ] OLD service stopped before copying the DBs
- [ ] Both DBs copied; `migrate_verify.py --compare` prints MATCH
- [ ] NEW service up; feed + chain + token verified through a session
- [ ] `install_autopull.sh` run on NEW; a test push to main redeploys on NEW
- [ ] OLD disabled (kept stopped, not terminated, for a few days)
