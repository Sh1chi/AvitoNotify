"""
OAuth-callback, health-check и подписка на веб-хуки.
"""
import os, httpx, time
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from db import get_pool
import config, auth

router = APIRouter()


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


@router.get("/callback/avito")
async def avito_callback(code: str):
    """
    OAuth-редирект для мультиаккаунтов.
    Получает токены, профиль пользователя, сохраняет аккаунт в БД и токены в хранилище.
    """
    # Обмен кода на токены
    tokens = await auth.exchange_code_for_tokens(code)
    # На случай, если Avito вернёт только expires_in, устанавливаем абсолютное время истечения
    tokens["expires_at"] = int(time.time()) + int(tokens.get("expires_in", 24*3600))

    # Получаем данные профиля, чтобы сохранить идентификатор и имя аккаунта
    me = await auth.fetch_self_info(tokens["access_token"])
    avito_user_id = int(me["id"])
    profile_name = me.get("name")

    # Создаём или обновляем запись аккаунта в БД
    async with (await get_pool()).acquire() as conn:
        await conn.execute(
            """
            INSERT INTO notify.accounts (avito_user_id, name, display_name)
            VALUES ($1, $2, $2)
            ON CONFLICT (avito_user_id) DO UPDATE
            SET name = EXCLUDED.name,
                display_name = COALESCE(notify.accounts.display_name, EXCLUDED.name)
            """,
            avito_user_id, profile_name,
        )

    # Сохраняем токены под конкретный avito_user_id
    await auth.store_tokens_for_user(avito_user_id, tokens)

    return {"ok": True, "avito_user_id": avito_user_id, "profile_name": profile_name}


@router.get("/oauth/avito/link")
def avito_link():
    """
    Возвращает готовую ссылку для авторизации пользователя в Avito.
    """
    return {"url": auth.build_authorize_url()}