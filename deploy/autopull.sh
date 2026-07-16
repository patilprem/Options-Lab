#!/usr/bin/env bash
# Keep the VPS on the latest origin/main automatically. Run by the
# optionslab-autopull.timer every 5 minutes; safe to run by hand too.
#
#   * no-op when already at origin/main (cheap: one git fetch)
#   * DEFERS updates during IST market hours (Mon-Fri 09:00-23:30, covering
#     NSE/BSE 09:00-15:35 AND MCX's later 09:00-23:30 session — restarting
#     mid-session drops the live feed/chain-recording connection, and MCX
#     chain data can't be re-fetched afterward) — FORCE=1 overrides
#   * bootstraps git in place on a non-git /opt/optionslab (scp-era installs):
#     tracked files are replaced, untracked files (*.db, *.duckdb, venv/,
#     node_modules/, push.local.ps1) are never touched
#   * hands off to deploy/deploy.sh (deps + frontend build + tests + restart);
#     failing tests abort before the restart, leaving the old code running
set -euo pipefail

# main() so bash parses the whole body BEFORE git replaces this very file
main() {
  local ROOT="${OPTIONSLAB_ROOT:-/opt/optionslab}"
  local REPO_URL="${OPTIONSLAB_REPO:-https://github.com/patilprem/Options-Lab.git}"
  local BRANCH="${OPTIONSLAB_BRANCH:-main}"
  cd "$ROOT"

  if [ ! -d .git ]; then
    echo "[autopull] no git repo in $ROOT — bootstrapping from $REPO_URL"
    git init -q
    git remote add origin "$REPO_URL"
  fi

  git fetch -q origin "$BRANCH"
  local local_sha remote_sha
  local_sha=$(git rev-parse HEAD 2>/dev/null || echo "none")
  remote_sha=$(git rev-parse "origin/$BRANCH")
  if [ "$local_sha" = "$remote_sha" ]; then
    echo "[autopull] up to date at ${local_sha:0:7}"
    return 0
  fi

  local now dow hm force
  now=$(TZ=Asia/Kolkata date +%u%H%M)   # e.g. 21012 = Tue 10:12
  dow=${now:0:1}
  hm=$((10#${now:1}))
  force="${FORCE:-0}"
  # An incoming commit tagged [force-deploy] overrides the market-hours
  # deferral — urgent fixes go live within one timer tick with no human on
  # the VPS. The offline test suite in deploy.sh still gates the restart.
  if [ "$force" != "1" ] && git log "${local_sha}..origin/${BRANCH}" \
      --format=%B 2>/dev/null | grep -q "\[force-deploy\]"; then
    echo "[autopull] incoming [force-deploy] marker — overriding market-hours deferral"
    force=1
  fi
  # 0900-2330 covers both NSE/BSE (close 1535) and MCX (close ~2330) so one
  # restart-safe window works for every segment this platform records.
  if [ "$force" != "1" ] && [ "$dow" -le 5 ] \
      && [ "$hm" -ge 900 ] && [ "$hm" -le 2330 ]; then
    echo "[autopull] ${local_sha:0:7} -> ${remote_sha:0:7} available but it is" \
         "IST market hours (NSE or MCX) — deferring (FORCE=1 bash deploy/autopull.sh to override)"
    return 0
  fi

  echo "[autopull] updating ${local_sha:0:7} -> ${remote_sha:0:7}"
  # -B resets the branch to origin even from an unborn HEAD (fresh bootstrap);
  # -f overwrites the old scp-era copies of tracked files
  git checkout -qf -B "$BRANCH" "origin/$BRANCH"
  bash deploy/deploy.sh
  echo "[autopull] now at $(git rev-parse --short HEAD)"
}

main "$@"
exit 0
