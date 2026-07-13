# push.ps1 — build the dashboard and deploy code + UI to the VPS in one shot.
# Run from anywhere:  powershell -File deploy\push.ps1
# Databases live at the repo ROOT on the server (/opt/optionslab/*.duckdb, *.db),
# NOT inside app/, so syncing app/ never touches your data.
#
# Server details load from deploy\push.local.ps1 (gitignored) so the repo never
# carries real IPs/paths. Create it once, next to this file:
#   $Key    = "C:\path\to\your-ssh-key.key"
#   $Server = "ubuntu@<your-vps-ip>"

$ErrorActionPreference = "Stop"
$local = Join-Path $PSScriptRoot "push.local.ps1"
if (-not (Test-Path $local)) {
    Write-Error "Missing deploy\push.local.ps1 — create it with `$Key and `$Server (see header)."
    exit 1
}
. $local
$Root   = "/opt/optionslab"
$Repo   = Split-Path $PSScriptRoot -Parent    # repo root (parent of deploy/)

Write-Host "==> [1/4] building dashboard..." -ForegroundColor Cyan
Push-Location "$Repo\frontend"
npm run build
Pop-Location

Write-Host "==> [2/4] clearing old dashboard on server..." -ForegroundColor Cyan
ssh -o StrictHostKeyChecking=accept-new -i $Key $Server "rm -rf $Root/app/static"

Write-Host "==> [3/4] uploading app code + dashboard (data untouched)..." -ForegroundColor Cyan
scp -i $Key -r "$Repo\app" "${Server}:$Root/"

Write-Host "==> [4/4] restarting service..." -ForegroundColor Cyan
ssh -i $Key $Server "sudo systemctl restart optionslab"

Write-Host "==> done. Just refresh the dashboard on your phone." -ForegroundColor Green
