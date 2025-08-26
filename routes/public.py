"""
OAuth-callback, health-check и подписка на веб-хуки.
"""
import os, httpx, time
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

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
    webhook_url = os.getenv("WEBHOOK_PUBLIC_URL")
    if not webhook_url:
        raise HTTPException(400, "WEBHOOK_PUBLIC_URL is not set")

    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(
            f"{config.AVITO_API_BASE}/messenger/v3/webhook",
            headers={"Authorization": f"Bearer {access}"},
            json={"url": os.getenv("WEBHOOK_PUBLIC_URL")},
        )
    if r.status_code not in (200, 201):
        raise HTTPException(r.status_code, r.text)
    return {"detail": "Webhook subscription OK", "avito_response": r.json()}


@router.get("/callback/avito", response_class=HTMLResponse)
async def avito_callback(code: str, request: Request):
    """
    OAuth-редирект для мультиаккаунтов.
    Получает токены, профиль пользователя, сохраняет аккаунт в БД и токены в хранилище.
    """
    try:
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


        # ── авто-подписка на webhook (реальный Avito требует только url)
        webhook_url = os.getenv("WEBHOOK_PUBLIC_URL") or (str(request.base_url).rstrip("/") + "/avito/webhook")
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                resp = await c.post(
                    f"{config.AVITO_API_BASE}/messenger/v3/webhook",
                    headers={"Authorization": f"Bearer {tokens['access_token']}"},
                    json={"url": webhook_url},
                )
                # быстрый health-check: эндпоинт должен отвечать 200 за <=2s
                try:
                    await c.post(webhook_url, json={"ping": True}, timeout=httpx.Timeout(2.0))
                except Exception:
                    pass  # необязателен для успешного OAuth
            # при не-200 не ломаем OAuth-страницу, просто можно залогировать resp.status_code/resp.text
        except Exception:
            pass


        return HTMLResponse(content=f"""
                <html>
                  <head>
                    <title>Avito Notify</title>
                  </head>
                  <body style="font-family: sans-serif; text-align: center; margin-top: 5em;">
                    <h2>✅ Авторизация успешна</h2>
                    <p>Аккаунт <b>{profile_name}</b> (ID {avito_user_id}) подключён.</p>
                    <p>Теперь вернитесь в Telegram и привяжите его к группе командой
                    <b>/link {avito_user_id}</b> в нужном чате.</p>
                  </body>
                </html>
                """)

    except Exception as e:
        return HTMLResponse(content=f"""
                <html>
                  <head>
                    <title>Avito Notify</title>
                  </head>
                  <body style="font-family: sans-serif; text-align: center; margin-top: 5em;">
                    <h2>❌ Ошибка авторизации</h2>
                    <p>{e}</p>
                  </body>
                </html>
                """, status_code=400)


@router.get("/oauth/avito/link")
def avito_link():
    """
    Возвращает готовую ссылку для авторизации пользователя в Avito.
    """
    return {"url": auth.build_authorize_url()}