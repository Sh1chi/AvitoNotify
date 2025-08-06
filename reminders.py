"""
Отправка периодических напоминаний, если продавец не ответил клиенту.
"""
from datetime import datetime, timedelta, timezone
from typing import Dict
import logging, httpx

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from auth import get_valid_access_token
from fastapi import FastAPI

import telegram, config

REMINDERS: Dict[int, dict] = {}  # chat_id → {"first_ts": datetime, "last_reminder": datetime|None}
log = logging.getLogger("AvitoNotify.reminders")


async def _last_message_status(chat_id: int) -> str:
    """
    Возвращает 'buyer' | 'seller' | 'unknown', анализируя
    GET /messenger/v2/accounts/{AVITO_USER_ID}/chats/{chat_id}.
    """
    token = await get_valid_access_token()
    url = (
        f"{config.AVITO_API_BASE}"
        f"/messenger/v2/accounts/{config.AVITO_USER_ID}/chats/{chat_id}"
    )

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    except Exception as exc:
        log.warning("Net-error on chat %s: %s", chat_id, exc)
        return "unknown"

    if r.status_code != 200:
        log.warning("Avito %s on chat %s: %s", r.status_code, chat_id, r.text[:120])
        return "unknown"

    data = r.json()
    last = data.get("last_message")
    if not last:
        return "unknown"

    return "buyer" if last.get("direction") == "in" else "seller"



async def remind_loop() -> None:
    """
    Проверяет, есть ли чаты без ответа дольше заданного интервала,
    и отправляет напоминание в Telegram.
    """
    now = datetime.now(timezone.utc)
    for chat_id, data in list(REMINDERS.items()):
        # Первое напоминание: время с момента сообщения >= REMIND_AFTER_MIN
        need_first = now - data["first_ts"] >= timedelta(minutes=config.REMIND_AFTER_MIN)

        # Повторное напоминание: если не было ещё ни одного
        # или с последнего прошло >= REMIND_AFTER_MIN
        need_next = (
            data["last_reminder"] is None
            or now - data["last_reminder"] >= timedelta(minutes=config.REMIND_AFTER_MIN)
        )

        if not (need_first and need_next):
            continue

        status = await _last_message_status(chat_id)

        if status == "buyer":
            minutes = int((now - data["first_ts"]).total_seconds() // 60)
            await telegram.send_telegram(
                f"⏰ Уже {minutes} мин без ответа в чате #{chat_id}"
            )
            data["last_reminder"] = now
            log.info("Reminder sent for chat %s", chat_id)

        elif status == "unknown":
            await telegram.send_telegram(
                f"⚠️ Не удалось получить данные по чату #{chat_id}. "
                "Пожалуйста, проверьте диалог вручную."
            )
            data["last_reminder"] = now
            log.info("Fallback reminder (unknown status) sent for chat %s", chat_id)

        elif status == "seller":
            # продавец уже ответил — webhook не сработал, убираем чат
            REMINDERS.pop(chat_id, None)
            log.info("Chat %s answered by seller (via API), reminder removed", chat_id)


def register(app: FastAPI) -> None:
    """
    Инициализирует планировщик (apscheduler) и запускает `remind_loop` каждую минуту.
    Вызывается в main.py при запуске приложения.
    """
    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(remind_loop, "interval", seconds=60, id="reminders")
    sched.start()
    log.info("Scheduler started (interval 60 s)")
