"""Avito OAuth bridge with detailed logging
Run with:
    uvicorn app:app --reload
Requirements:
    fastapi[all]
    httpx
    python-dotenv
"""

import json
import os
import time
import logging
from pathlib import Path
from typing import Dict, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("avito_bridge")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv(".env")  # expects AVITO_CLIENT_ID, AVITO_CLIENT_SECRET, AVITO_REDIRECT_URI

CLIENT_ID: str | None = os.getenv("AVITO_CLIENT_ID")
CLIENT_SECRET: str | None = os.getenv("AVITO_CLIENT_SECRET")
REDIRECT_URI: str | None = os.getenv("AVITO_REDIRECT_URI")
TOKEN_URL = os.getenv("AVITO_TOKEN_URL", "https://api.avito.ru/token")
AVITO_API_BASE = os.getenv("AVITO_API_BASE", "https://api.avito.ru")

if not all([CLIENT_ID, CLIENT_SECRET, REDIRECT_URI]):
    logger.warning("Environment variables AVITO_CLIENT_ID / AVITO_CLIENT_SECRET / AVITO_REDIRECT_URI are not fully set")

TOKENS_FILE = Path("tokens.json")  # simplest persistence

# ---------------------------------------------------------------------------
# Helper functions for token storage
# ---------------------------------------------------------------------------

def load_tokens() -> Optional[Dict]:
    if not TOKENS_FILE.exists():
        return None
    try:
        with TOKENS_FILE.open() as f:
            data = json.load(f)
    except json.JSONDecodeError:
        logger.error("tokens.json повреждён – удаляю, требуется авторизация")
        TOKENS_FILE.unlink(missing_ok=True)
        return None

    # минимальная валидация
    if not {"access_token", "refresh_token", "expires_at"} <= data.keys():
        logger.error("tokens.json неполный – удаляю, требуется авторизация")
        TOKENS_FILE.unlink(missing_ok=True)
        return None
    return data


def save_tokens(tokens: Dict) -> None:
    # 1. backup
    if TOKENS_FILE.exists():
        TOKENS_FILE.replace(TOKENS_FILE.with_suffix(".bak"))

    # 2. atomic write
    tmp = TOKENS_FILE.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(tokens, f, indent=2)
    tmp.replace(TOKENS_FILE)

    # 3. chmod 600
    try:
        os.chmod(TOKENS_FILE, 0o600)
    except PermissionError:
        pass

    logger.info("Tokens saved: access_token=*** refresh_token=*** expires_at=%s", tokens["expires_at"])

# ---------------------------------------------------------------------------
# Token exchange & refresh
# ---------------------------------------------------------------------------

async def exchange_code_for_tokens(code: str) -> Dict:
    """Exchange one‑time `code` for access/refresh tokens."""
    logger.info("Exchanging code '%s' for tokens", code)
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": REDIRECT_URI,
            },
        )
    if resp.status_code != 200:
        logger.error("Token exchange failed: %s", resp.text)
        raise HTTPException(resp.status_code, f"Avito token error: {resp.text}")

    data = resp.json()
    tokens = {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": int(time.time()) + data["expires_in"],
    }
    logger.info("Received tokens (access_token=***, refresh_token=***, expires_in=%s)", data["expires_in"])
    return tokens


async def refresh_if_needed(tokens: Dict) -> Dict:
    """Ensure access_token is valid; refresh if <60s left."""
    now = int(time.time())
    if now < tokens["expires_at"] - 60:
        logger.debug("Access token still valid for %s s", tokens["expires_at"] - now)
        return tokens

    logger.info("Access token expiring or expired — refreshing via refresh_token")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
        )

    if resp.status_code != 200:
        logger.warning("Refresh token invalid: %s", resp.text)
        raise HTTPException(401, f"Need new OAuth: {resp.text}")

    data = resp.json()
    new_tokens = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", tokens["refresh_token"]),
        "expires_at": now + data["expires_in"],
    }
    logger.info("Token refresh successful; expires in %s s", data["expires_in"])
    save_tokens(new_tokens)
    return new_tokens

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Avito OAuth bridge with logging")

# Middleware to log every request/response
@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info("--> %s %s", request.method, request.url.path)
    response = await call_next(request)
    logger.info("<-- %s %s", response.status_code, request.url.path)
    return response


@app.get("/oauth/callback")
async def oauth_callback(code: str):
    """Endpoint set as redirect_uri in Avito: handles ?code=…"""
    tokens = await exchange_code_for_tokens(code)
    save_tokens(tokens)
    return {"detail": "Авторизация успешна. Бот готов работать 🎉"}


async def get_valid_access_token() -> str:
    stored = load_tokens()
    if not stored:
        raise HTTPException(401, "Нет токенов. Пройдите авторизацию.")

    # если refresh_if_needed по какой-то причине вернул «битый» словарь
    fresh = await refresh_if_needed(stored)
    try:
        return fresh["access_token"]
    except KeyError:
        logger.error("В tokens.json отсутствует access_token – нужна авторизация")
        raise HTTPException(401, "Нет токенов. Пройдите авторизацию.")


@app.get("/ping-avito")
async def ping_avito(access: str = Depends(get_valid_access_token)):
    """Example protected call to Avito API (or mock)."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{AVITO_API_BASE}/messenger/v2/chats",
            headers={"Authorization": f"Bearer {access}"},
        )

    logger.info("Ping Avito returned %s", r.status_code)
    return {
        "status_code": r.status_code,
        "response_sample": r.json() if r.status_code == 200 else r.text,
    }

