"""
Dhan Token Manager
==================
SEBI rules: access tokens are valid for 24 hours. This module makes the
daily refresh a 20-second phone tap instead of a chore, and keeps the
platform honest about token health.

Setup (once, on the DhanHQ portal — the screen you screenshotted):
  1. Toggle to "API Key" mode and Generate new API Key with:
       Application name : OptionsLab
       Redirect URL     : https://<your-server>/dhan/callback
       Postback URL     : (optional)
  2. Set up TOTP under Optional Settings (makes daily login quick + 2FA).
  3. Add your server's Static IP under Static IP Setting.

Daily flow (fully unattended path first, phone-tap fallback second):
  08:30 IST scheduler wakes ->
    token still valid past market close?  -> do nothing
    else:
      a) generate a consent/login URL from api_key + secret
      b) send it to your phone via ntfy.sh push (or log it)
      c) you tap the link, log in with PIN/TOTP on Dhan's page
      d) Dhan redirects to /dhan/callback?tokenId=... on this server
      e) server consumes tokenId -> access token stored in SQLite
  All Dhan calls read the token via get_access_token(); the UI shows a
  countdown chip and a Refresh action that re-sends the login link.

Env / config:
  DHAN_CLIENT_ID, DHAN_API_KEY (app id), DHAN_API_SECRET,
  NTFY_TOPIC (optional, e.g. "optionslab-<random>"; subscribe in the
  ntfy app on your phone), BASE_URL (your server URL for the callback).
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

DB_PATH = Path(__file__).resolve().parents[2] / "optionslab.db"
IST = timezone(timedelta(hours=5, minutes=30))

CLIENT_ID = os.environ.get("DHAN_CLIENT_ID", "")
API_KEY = os.environ.get("DHAN_API_KEY", "")
API_SECRET = os.environ.get("DHAN_API_SECRET", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000")

TOKEN_LIFETIME_H = 24

router = APIRouter(tags=["token"])


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def _init():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS dhan_token (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            access_token TEXT, generated_at TEXT, expires_at TEXT)""")


def _save(token: str) -> None:
    _init()
    now = datetime.now(IST)
    with sqlite3.connect(DB_PATH) as c:
        c.execute("INSERT OR REPLACE INTO dhan_token VALUES (1, ?, ?, ?)",
                  (token, now.isoformat(),
                   (now + timedelta(hours=TOKEN_LIFETIME_H)).isoformat()))


def _load() -> dict | None:
    _init()
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT access_token, generated_at, expires_at "
                        "FROM dhan_token WHERE id=1").fetchone()
    if not row or not row[0]:
        return None
    return {"access_token": row[0], "generated_at": row[1], "expires_at": row[2]}


def get_access_token() -> str:
    """Every Dhan API call should get its token from here."""
    t = _load()
    if not t:
        raise RuntimeError("No Dhan access token. Open the dashboard and refresh the token.")
    if datetime.fromisoformat(t["expires_at"]) <= datetime.now(IST):
        raise RuntimeError("Dhan access token expired. Refresh from the dashboard.")
    return t["access_token"]


def token_status() -> dict:
    t = _load()
    if not t:
        return {"state": "missing", "hours_left": 0, "expires_at": None}
    exp = datetime.fromisoformat(t["expires_at"])
    left = (exp - datetime.now(IST)).total_seconds() / 3600
    market_close = datetime.now(IST).replace(hour=15, minute=35, second=0)
    state = "ok" if exp > market_close else ("expiring" if left > 0 else "expired")
    return {"state": state, "hours_left": round(max(0, left), 1),
            "expires_at": t["expires_at"]}


# ---------------------------------------------------------------------------
# login-link generation + notification
# ---------------------------------------------------------------------------

def build_login_url() -> str:
    """Create a consent session with your API key/secret and return the
    browser login URL. Uses the official dhanhq client when installed."""
    if not (API_KEY and API_SECRET):
        raise RuntimeError("Set DHAN_API_KEY and DHAN_API_SECRET env vars first.")
    try:
        from dhanhq import DhanLogin
        login = DhanLogin(CLIENT_ID)
        consent_app_id = login.generate_login_session(API_KEY, API_SECRET)
        # INDIVIDUAL flow: /login/consentApp-login?consentAppId=...
        # (the /consent-login?consentId=... form is the PARTNER flow — an
        # individual API key gets "unauthorized" there. Live-verified.)
        return (f"https://auth.dhan.co/login/consentApp-login"
                f"?consentAppId={consent_app_id}")
    except ImportError as e:
        raise RuntimeError("pip install dhanhq to enable token generation") from e


def notify_phone(url: str) -> bool:
    """Push the login link to your phone via ntfy.sh (free, no account:
    install the ntfy app, subscribe to your NTFY_TOPIC)."""
    if not NTFY_TOPIC:
        print(f"[token] login link (set NTFY_TOPIC for phone push): {url}")
        return False
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}",
                      data=f"OptionsLab: tap to refresh Dhan token\n{url}",
                      headers={"Title": "Dhan token refresh",
                               "Priority": "high", "Click": url},
                      timeout=10)
        return True
    except Exception as e:
        print(f"[token] ntfy push failed: {e}")
        return False


async def daily_refresh_loop():
    """Background task: at 08:30 IST every day, if the token won't survive
    until market close, generate a login link and push it to your phone."""
    import asyncio
    while True:
        now = datetime.now(IST)
        target = now.replace(hour=8, minute=30, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        st = token_status()
        if st["state"] != "ok":
            try:
                notify_phone(build_login_url())
            except Exception as e:
                print(f"[token] daily refresh failed: {e}")


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

class ManualToken(BaseModel):
    access_token: str


@router.get("/token/status")
def status():
    return token_status()


@router.post("/token/refresh")
def refresh():
    """Called by the dashboard's Refresh button: builds a fresh login link,
    pushes it to your phone, and returns it so the browser can open it."""
    try:
        url = build_login_url()
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    pushed = notify_phone(url)
    return {"login_url": url, "pushed_to_phone": pushed}


@router.get("/dhan/callback")
def dhan_callback(tokenId: str = ""):
    """Set this route as the Redirect URL on the DhanHQ portal.
    Dhan redirects here after you log in; we consume tokenId -> token."""
    if not tokenId:
        raise HTTPException(400, "missing tokenId")
    try:
        from dhanhq import DhanLogin
        login = DhanLogin(CLIENT_ID)
        resp = login.consume_token_id(tokenId, API_KEY, API_SECRET)
    except Exception as e:
        raise HTTPException(502, f"token exchange failed: {e}")
    # the SDK returns the whole response dict; the JWT is inside it
    access_token = (resp.get("accessToken") or resp.get("access_token")
                    if isinstance(resp, dict) else resp)
    if not access_token or not isinstance(access_token, str):
        raise HTTPException(502, f"no accessToken in exchange response: {resp}")
    _save(access_token)
    return {"ok": True, "message": "Token refreshed. You can close this tab.",
            **token_status()}


@router.post("/token/manual")
def manual(body: ManualToken):
    """Fallback: paste a token generated on the Dhan portal directly."""
    _save(body.access_token.strip())
    return token_status()
