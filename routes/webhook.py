"""
Приём Avito-webhook’ов и постановка напоминаний
"""
import base64, hashlib, hmac, logging
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request

import config, telegram
from reminders import REMINDERS

router = APIRouter()
log = logging.getLogger("avito_bridge.webhook")


def _verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """
    Проверяет корректность HMAC-SHA256 подписи от Avito webhook.
    """
    calc = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(calc).decode(), signature)


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
    ts = datetime.fromtimestamp(event["timestamp"], tz=timezone.utc)\
                 .strftime("%Y-%m-%d %H:%M:%S UTC")

    # продавец ответил — убираем напоминание
    if author == seller:
        REMINDERS.pop(chat_id, None)
        return {"ok": True}

    # Клиент написал — шлём уведомление в Telegram
    msg = (
        "📩 *Новое сообщение Avito*\n"
        f"Чат #{chat_id}\n"
        f"Текст: {text}\n"
        f"Время: {ts}"
    )
    await telegram.send_telegram(msg)

    # Ставим напоминание, если его ещё нет
    REMINDERS.setdefault(
        chat_id, {"first_ts": datetime.now(timezone.utc), "last_reminder": None}
    )
    return {"ok": True}
