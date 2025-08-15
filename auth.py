"""
Токены Avito — загрузка/сохранение, получение, обновление (exchange & refresh).
"""
import json, os, time, logging, httpx, asyncio
from typing import Dict, Optional
from urllib.parse import urlencode

import config

log = logging.getLogger("AvitoNotify.auth")

CLIENT_CRED_KEY = "__client_credentials__"
_TOK_LOCK = asyncio.Lock()  # синхронизация при одновременном доступе к файлу токенов


def build_authorize_url(state: str | None = None) -> str:
    """
    Формирует URL для авторизации в Avito (authorization_code flow).
    """
    params = {
        "response_type": "code",
        "client_id": config.CLIENT_ID,
        "redirect_uri": config.REDIRECT_URI,
        "scope": config.AVITO_OAUTH_SCOPES,
    }
    if state:
        params["state"] = state
    return "https://avito.ru/oauth?" + urlencode(params)


async def fetch_self_info(access_token: str) -> dict:
    """
    Получает профиль текущего пользователя Avito по access_token.
    """
    url = f"{config.AVITO_API_BASE}/core/v1/accounts/self"
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {access_token}"})
    r.raise_for_status()
    return r.json()


def _store_upsert(key: str, rec: dict) -> None:
    """Обновляет или добавляет запись токенов в файл."""
    store = _read_store(); store[key] = rec; _write_store(store)


async def get_app_access_token() -> str:
    """
    Получает access_token по client_credentials (персональная авторизация).
    Токен живёт ~24ч, refresh отсутствует — при истечении запрашивается заново.
    """
    async with _TOK_LOCK:
        store = _read_store()
        rec = store.get(CLIENT_CRED_KEY)
        if rec and not _expired(rec.get("expires_at"), skew=60):
            return rec["access_token"]

        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                config.TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": config.CLIENT_ID,
                    "client_secret": config.CLIENT_SECRET,
                    # у некоторых интеграций scope опционален; если ваш кабинет требует — оставьте:
                    "scope": config.AVITO_OAUTH_SCOPES,
                },
            )
        if r.status_code != 200:
            raise RuntimeError(f"client_credentials failed: {r.status_code} {r.text}")

        body = r.json()
        rec = {
            "access_token": body["access_token"],
            "expires_at": _now() + int(body.get("expires_in", 24*3600)),
        }
        _store_upsert(CLIENT_CRED_KEY, rec)
        return rec["access_token"]


def _now() -> int:
    """Текущее время в секундах UNIX."""
    return int(time.time())


def _expired(expires_at: int | None, skew: int = 60) -> bool:
    """Проверяет, что срок жизни токена истёк или истечёт в ближайшие skew секунд."""
    return not expires_at or _now() >= (expires_at - skew)


def _read_store() -> dict:
    """Читает JSON-хранилище токенов с диска."""
    if not config.TOKENS_FILE.exists():
        return {}
    try:
        return json.loads(config.TOKENS_FILE.read_text("utf-8"))
    except Exception:
        return {}


def _write_store(store: dict) -> None:
    """Сохраняет JSON-хранилище токенов на диск."""
    config.TOKENS_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


async def exchange_code_for_tokens(code: str) -> Dict:
    """
    Обменивает одноразовый code от Avito на access/refresh токены.
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


async def get_valid_access_token(avito_user_id: int) -> str:
    """
    Возвращает валидный access_token для конкретного avito_user_id.
    Обновляет токен, если он истёк, через refresh_token.
    """
    async with _TOK_LOCK:
        store = _read_store()
        key = str(avito_user_id)
        rec = store.get(key)

        if not rec:
            # нет пользовательских токенов — попробуем персональный режим (owner)
            if config.AVITO_OWNER_USER_ID and avito_user_id == config.AVITO_OWNER_USER_ID:
                return await get_app_access_token()
            raise RuntimeError(f"No tokens stored for avito_user_id={avito_user_id}")

        if not _expired(rec.get("expires_at")):
            return rec["access_token"]

        # refresh flow
        refresh_token = rec.get("refresh_token")
        if not refresh_token:
            # это может быть запись client_credentials старого формата — получить заново
            if config.AVITO_OWNER_USER_ID and avito_user_id == config.AVITO_OWNER_USER_ID:
                return await get_app_access_token()
            raise RuntimeError(f"No refresh_token for avito_user_id={avito_user_id}")

        # обычный refresh по authorization_code
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                config.TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": config.CLIENT_ID,
                    "client_secret": config.CLIENT_SECRET,
                },
            )
        if r.status_code != 200:
            raise RuntimeError(f"Refresh failed for {avito_user_id}: {r.status_code} {r.text}")

        data = r.json()
        rec = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", refresh_token),
            "expires_at": _now() + int(data.get("expires_in", 3600)),
        }
        store[key] = rec
        _write_store(store)
        return rec["access_token"]


async def store_tokens_for_user(avito_user_id: int, tokens: dict) -> None:
    """Сохраняет токены конкретного пользователя Avito в файл."""
    _store_upsert(str(avito_user_id), {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "expires_at": tokens["expires_at"],
    })