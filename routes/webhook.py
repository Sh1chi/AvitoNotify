"""
Приём Avito-webhook’ов и постановка напоминаний
"""
import base64, hashlib, hmac, logging
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request
from dataclasses import dataclass

import config, telegram
from db import get_pool

router = APIRouter()
log = logging.getLogger("AvitoNotify.webhook")


@dataclass
class EventData:
    seller: int
    author: int
    chat_id: str
    text: str
    ts_str: str


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
    Обрабатывает входящий webhook от Avito.
    """
    raw = await request.body()
    _check_signature(raw, request.headers.get("X-Hook-Signature", ""))

    event_data = await _parse_event(request)
    account_id = await _ensure_account(event_data.seller)

    if _is_seller_reply(event_data):
        await _remove_reminder(account_id, event_data.chat_id)
        return {"ok": True}

    await _notify_all_chats(account_id, event_data)
    await _add_reminder(account_id, event_data.chat_id)
    return {"ok": True}


def _check_signature(raw_body: bytes, signature: str):
    """Выбрасывает 401, если подпись неверна."""
    if not _verify_signature(raw_body, signature, config.AVITO_HOOK_SECRET):
        raise HTTPException(401, "Bad signature")


async def _parse_event(request: Request):
    """Достаёт seller, author, chat_id, текст, timestamp."""
    event = await request.json()
    value = event.get("payload", {}).get("value", {})
    return EventData(
        seller=int(value.get("user_id", 0)),
        author=int(value.get("author_id", 0)),
        chat_id=str(value.get("chat_id", "")),
        text=value.get("content", {}).get("text", "[пусто]"),
        ts_str=datetime.fromtimestamp(event["timestamp"], tz=timezone.utc)
                      .strftime("%Y-%m-%d %H:%M:%S UTC")
    )


def _is_seller_reply(event_data: EventData) -> bool:
    """Определяет, что это ответ продавца."""
    return event_data.author == event_data.seller


async def _remove_reminder(account_id: int, chat_id: str):
    """Удаляет напоминание по чату."""
    async with (await get_pool()).acquire() as conn:
        await conn.execute(
            "DELETE FROM reminders WHERE account_id=$1 AND avito_chat_id=$2",
            account_id, chat_id
        )


async def _notify_all_chats(account_id: int, event_data: EventData):
    """Шлёт сообщение во все связанные чаты."""
    msg = (
        "📩 *Новое сообщение Avito*\n"
        f"Аккаунт: {event_data.seller}\n"
        f"Чат #{event_data.chat_id}\n"
        f"Текст: {event_data.text}\n"
        f"Время: {event_data.ts_str}"
    )
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch("""
            SELECT tg_chat_id
            FROM v_account_chat_targets
            WHERE account_id=$1 AND muted=FALSE
        """, account_id)
    for r in rows:
        await telegram.send_telegram_to(msg, r["tg_chat_id"])


async def _add_reminder(account_id: int, chat_id: str):
    """Ставит напоминание о непрочитанном сообщении."""
    async with (await get_pool()).acquire() as conn:
        await conn.execute("""
            INSERT INTO reminders (account_id, avito_chat_id, first_ts)
            VALUES ($1, $2, now())
            ON CONFLICT (account_id, avito_chat_id) DO NOTHING
        """, account_id, chat_id)
