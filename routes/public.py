"""
OAuth-callback, health-check и подписка на веб-хуки.
"""
import os, httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

import config, auth

router = APIRouter()


@router.get("/oauth/callback")
async def oauth_callback(code: str):
    """
    Обрабатывает редирект от Avito OAuth.
    Обменивает код на токены и сохраняет их локально.
    """
    tokens = await auth.exchange_code_for_tokens(code)
    import auth as _internal
    _internal._save_tokens(tokens)
    return {"detail": "Авторизация успешна. Бот готов работать 🎉"}


@router.get("/ping-avito")
async def ping_avito(access: str = Depends(auth.get_valid_access_token)):
    """
    Проверяет работоспособность Avito API с действующим access_token.
    Можно использовать как health-check.
    """
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(
            f"{config.AVITO_API_BASE}/messenger/v2/chats",
            headers={"Authorization": f"Bearer {access}"},
        )
    return {
        "status_code": r.status_code,
        "response_sample": r.json() if r.status_code == 200 else r.text,
    }


@router.post("/subscribe-avito-webhook")
async def subscribe_webhook(
    background: BackgroundTasks,
    access: str = Depends(auth.get_valid_access_token),
):
    """
    Подписывает текущий сервис на получение webhook'ов от Avito.
    URL берётся из переменной окружения WEBHOOK_PUBLIC_URL.
    """
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(
            f"{config.AVITO_API_BASE}/messenger/v3/webhook",
            headers={"Authorization": f"Bearer {access}"},
            json={"url": os.getenv("WEBHOOK_PUBLIC_URL")},
        )
    if r.status_code not in (200, 201):
        raise HTTPException(r.status_code, r.text)
    return {"detail": "Webhook subscription OK", "avito_response": r.json()}
