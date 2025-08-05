"""
Отправка периодических напоминаний, если продавец не ответил клиенту.
"""
from datetime import datetime, timedelta, timezone
from typing import Dict
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

import telegram, config

REMINDERS: Dict[int, dict] = {}  # chat_id → {"first_ts": datetime, "last_reminder": datetime|None}
log = logging.getLogger("AvitoNotify.reminders")


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
        if need_first and need_next:
            delta = now - data["first_ts"]
            minutes = int(delta.total_seconds() // 60)
            await telegram.send_telegram(f"⏰ Уже {minutes} мин без ответа в чате #{chat_id}")
            data["last_reminder"] = now
            log.info("Reminder sent for chat %s", chat_id)


def register(app: FastAPI) -> None:
    """
    Инициализирует планировщик (apscheduler) и запускает `remind_loop` каждую минуту.
    Вызывается в main.py при запуске приложения.
    """
    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(remind_loop, "interval", seconds=60, id="reminders")
    sched.start()
    log.info("Scheduler started (interval 60 s)")
