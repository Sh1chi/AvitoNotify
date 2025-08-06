"""
Приём Avito-webhook’ов и постановка напоминаний
"""
import base64, hashlib, hmac, logging
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request

import config, telegram
from db import get_pool
from reminders import REMINDERS

router = APIRouter()
log = logging.getLogger("avito_bridge.webhook")


def _verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """
    Проверяет корректность HMAC-SHA256 подписи от Avito webhook.
    """
    calc = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(calc).decode(), signature)


async def _ensure_account(avito_user_id: int) -> int:
    """
    Возвращает internal `account_id`, создавая запись при первом веб-хуке.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO accounts (avito_user_id) VALUES ($1) "
            "ON CONFLICT (avito_user_id) DO UPDATE SET avito_user_id = EXCLUDED.avito_user_id "
            "RETURNING id",
            avito_user_id,
        )
    return row["id"]


@router.post("/avito/webhook")
async def avito_webhook(request: Request):
    """
    Обрабатывает входящий webhook от Avito:
    - Проверяет подпись;
    - Распознаёт отправителя (продавец или клиент);
    - Отправляет уведомление в Telegram;
    - Добавляет напоминание, если продавец не ответил.
    """
    raw = await request.body()
    if not _verify_signature(
        raw, request.headers.get("X-Hook-Signature", ""), config.AVITO_HOOK_SECRET
    ):
        raise HTTPException(401, "Bad signature")

    # Извлекаем данные события
    event   = await request.json()
    value   = event.get("payload", {}).get("value", {})
    chat_id = int(value.get("chat_id", 0))
    author  = int(value.get("author_id", 0))  # отправитель сообщения
    seller  = int(value.get("user_id", 0))    # владелец webhook'а
    text    = value.get("content", {}).get("text", "[пусто]")

    # Читаемый timestamp
    ts_str = datetime.fromtimestamp(event["timestamp"], tz=timezone.utc)\
                 .strftime("%Y-%m-%d %H:%M:%S UTC")

    account_id = await _ensure_account(seller)
    pool = await get_pool()

    # ───── продавец ответил → удаляем напоминание ──────────────────
    if author == seller:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM reminders WHERE account_id=$1 AND chat_id=$2",
                account_id,
                chat_id,
            )
        return {"ok": True}

    # ───── покупатель написал → уведомляем и ставим напоминание ────
    msg = (
        "📩 *Новое сообщение Avito*\n"
        f"Аккаунт: {seller}\n"
        f"Чат #{chat_id}\n"
        f"Текст: {text}\n"
        f"Время: {ts_str}"
    )
    await telegram.send_telegram(msg)

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO reminders (account_id, chat_id, first_ts)
            VALUES ($1, $2, now())
            ON CONFLICT (account_id, chat_id) DO NOTHING
            """,
            account_id,
            chat_id,
        )
    return {"ok": True}
