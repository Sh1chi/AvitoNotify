"""
Токены Avito — загрузка/сохранение, exchange & refresh.
"""
import json, os, time, logging, httpx
from typing import Dict, Optional

import config

log = logging.getLogger("AvitoNotify.auth")


def _load_tokens() -> Optional[Dict]:
    """
    Загружает токены из файла. Если файл повреждён или неполный — удаляет его.
    """
    if not config.TOKENS_FILE.exists():
        return None
    try:
        with config.TOKENS_FILE.open() as f:
            data = json.load(f)
    except json.JSONDecodeError:
        log.error("tokens.json повреждён: удаляю")
        config.TOKENS_FILE.unlink(missing_ok=True)
        return None

    if not {"access_token", "refresh_token", "expires_at"} <= data.keys():
        log.error("tokens.json неполный: удаляю")
        config.TOKENS_FILE.unlink(missing_ok=True)
        return None
    return data


def _save_tokens(tokens: Dict) -> None:
    """
    Безопасно сохраняет токены в файл:
    - атомарная запись (tmp → rename),
    - права доступа 0600 (если возможно),
    - логирует время истечения.
    """
    tmp = config.TOKENS_FILE.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(tokens, f, indent=2)
    tmp.replace(config.TOKENS_FILE)
    try:
        os.chmod(config.TOKENS_FILE, 0o600)
    except PermissionError:
        pass
    log.info("Tokens saved; expires_at=%s", tokens["expires_at"])


async def exchange_code_for_tokens(code: str) -> Dict:
    """
    Обменивает одноразовый `code` от Avito на access/refresh токены.
    Бросает исключение, если ответ не 200.
    """
    log.info("Exchanging code '%s' for tokens", code)
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(
            config.TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": config.CLIENT_ID,
                "client_secret": config.CLIENT_SECRET,
                "redirect_uri": config.REDIRECT_URI,
            },
        )
    if r.status_code != 200:
        log.error("Token exchange failed: %s", r.text)
        raise httpx.HTTPStatusError(r.text, request=r.request, response=r)

    body = r.json()
    tokens = {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "expires_at": int(time.time()) + body["expires_in"],
    }
    log.info("Received tokens (access_token=***, refresh_token=***, expires_in=%s)", body["expires_in"])
    return tokens


async def _refresh(tokens: Dict) -> Dict:
    """
    Обновляет access_token, если он истёк или скоро истечёт.
    Сохраняет новые токены, если обновление успешно.
    """
    log.info("Access token expiring or expired — refreshing via refresh_token")
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(
            config.TOKEN_URL,
            data={
                "grant_type":    "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id":     config.CLIENT_ID,
                "client_secret": config.CLIENT_SECRET,
            },
        )
    if r.status_code != 200:
        log.warning("Refresh token invalid: %s", r.text)
        raise httpx.HTTPStatusError(r.text, request=r.request, response=r)

    body = r.json()
    new_tokens = {
        "access_token":  body["access_token"],
        "refresh_token": body.get("refresh_token", tokens["refresh_token"]),
        "expires_at":    int(time.time()) + body["expires_in"],
    }
    _save_tokens(new_tokens)
    log.info("Token refreshed; expires_in=%s", body["expires_in"])
    return new_tokens


async def get_valid_access_token() -> str:
    """
    FastAPI-зависимость: гарантирует валидный access_token.
    Если токен отсутствует или просрочен — 401 или автоматическое обновление.
    """
    stored = _load_tokens()
    if not stored:
        from fastapi import HTTPException
        raise HTTPException(401, "Нет токенов. Авторизуйтесь через /oauth/callback.")
    # refresh при необходимости
    if int(time.time()) >= stored["expires_at"] - 60:
        stored = await _refresh(stored)
    return stored["access_token"]
