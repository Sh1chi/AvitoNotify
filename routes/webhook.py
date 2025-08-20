"""
Приём Avito-webhook’ов и постановка напоминаний
"""
import base64, hashlib, hmac, logging
from datetime import datetime, timezone, time as dtime
from zoneinfo import ZoneInfo
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
    await _broadcast_to_working_chats(account_id, event_data)


async def _add_reminder(account_id: int, chat_id: str):
    """Ставит напоминание о непрочитанном сообщении."""
    async with (await get_pool()).acquire() as conn:
        await conn.execute("""
            INSERT INTO reminders (account_id, avito_chat_id, first_ts)
            VALUES ($1, $2, now())
            ON CONFLICT (account_id, avito_chat_id) DO NOTHING
        """, account_id, chat_id)


def _in_window(local: dtime, start: dtime | None, end: dtime | None) -> bool:
    """Проверяет попадание локального времени в окно [start, end) с поддержкой «через полночь».
       Если окно не задано — считаем 24/7."""
    if not start or not end:
        return True
    if start == end:
        return True
    if start < end:
        return start <= local < end
    return local >= start or local < end


def _parse_utc(ts_str: str) -> datetime:
    """Парсит строку вида 'YYYY-MM-DD HH:MM:SS UTC' в aware-datetime (UTC)."""
    base = ts_str.replace(" UTC", "")
    dt = datetime.strptime(base, "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=timezone.utc)


async def _broadcast_to_working_chats(account_id: int, event_data: EventData) -> None:
    """Шлёт сообщение только тем чатам аккаунта, у кого сейчас рабочее время."""
    now_utc = datetime.now(timezone.utc)
    msg_utc_dt = _parse_utc(event_data.ts_str)

    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                ch.tg_chat_id,
                l.work_from, l.work_to, l.tz, l.muted,
                COALESCE(a.display_name, a.name, a.avito_user_id::text) AS account_label
            FROM notify.account_chat_links l
            JOIN notify.telegram_chats ch ON ch.id = l.chat_id
            JOIN notify.accounts a       ON a.id = l.account_id
            WHERE l.account_id = $1 AND l.muted = FALSE
            """,
            account_id,
        )

    for r in rows:
        tzname = r["tz"] or "UTC"

        # проверка рабочих часов — по ТЕКУЩЕМУ локальному времени
        local_now = now_utc.astimezone(ZoneInfo(tzname)).time().replace(second=0, microsecond=0)
        if not _in_window(local_now, r["work_from"], r["work_to"]):
            log.info(
                "skip off-hours: chat=%s tz=%s now=%s window=%s–%s",
                r["tg_chat_id"], tzname, local_now, r["work_from"], r["work_to"]
            )
            continue

        local_msg_time = msg_utc_dt.astimezone(ZoneInfo(tzname)).strftime("%d.%m.%Y %H:%M")

        text = (
            "📩 *Новое сообщение Avito*\n"
            f"Аккаунт: {r['account_label']}\n"
            f"Чат #{event_data.chat_id}\n"
            f"Текст: {event_data.text}\n"
            f"Время: {local_msg_time}"
        )
        await telegram.send_telegram_to(text, r["tg_chat_id"])