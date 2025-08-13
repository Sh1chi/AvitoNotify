"""
Отправка периодических напоминаний, если продавец не ответил клиенту.
"""
from datetime import datetime, timezone, timedelta
from db import get_pool
import logging, httpx

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from auth import get_valid_access_token
_scheduler: AsyncIOScheduler | None = None  # ← добавь выше, рядом с логгером

import telegram, config

log = logging.getLogger("AvitoNotify.reminders")


async def _last_message_status(avito_chat_id: int) -> str:
    """
    Возвращает 'buyer' | 'seller' | 'unknown', анализируя
    GET /messenger/v2/accounts/{AVITO_USER_ID}/chats/{avito_chat_id}.
    """
    token = await get_valid_access_token()
    url = (
        f"{config.AVITO_API_BASE}"
        f"/messenger/v2/accounts/{config.AVITO_USER_ID}/chats/{avito_chat_id}"
    )

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    except Exception as exc:
        log.warning("Net-error on chat %s: %s", avito_chat_id, exc)
        return "unknown"

    if r.status_code != 200:
        log.warning("Avito %s on chat %s: %s", r.status_code, avito_chat_id, r.text[:120])
        return "unknown"

    data = r.json()
    last = data.get("last_message")
    if not last:
        return "unknown"

    return "buyer" if last.get("direction") == "in" else "seller"


async def remind_loop() -> None:
    """
    раз в минуту: берёт активные напоминания из БД,
    проверяет последнее сообщение и/или шлёт уведомление
    """
    now = datetime.now(timezone.utc)
    sql_due = """
        SELECT account_id, avito_chat_id, first_ts, last_reminder
        FROM   reminders
        WHERE  (last_reminder IS NULL OR $1 - last_reminder >= $2)
        """
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(sql_due, now, timedelta(minutes=config.REMIND_AFTER_MIN))

    for row in rows:
        status = await _last_message_status(row["avito_chat_id"])
        pool = await get_pool()

        if status == "buyer":
            minutes = int((now - row["first_ts"]).total_seconds() // 60)
            await telegram.send_telegram(
                f"⏰ Уже {minutes} мин без ответа в чате #{row['avito_chat_id']}"
            )
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE reminders SET last_reminder = $1 WHERE account_id=$2 AND avito_chat_id=$3",
                    now,
                    row["account_id"],
                    row["avito_chat_id"],
                )

        elif status == "unknown":
            await telegram.send_telegram(
                f"⚠️ Не удалось получить данные по чату #{row['avito_chat_id']}. "
                "Проверьте вручную."
            )
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE reminders SET last_reminder = $1 WHERE account_id=$2 AND avito_chat_id=$3",
                    now,
                    row["account_id"],
                    row["avito_chat_id"],
                )

        elif status == "seller":
            # продавец ответил – убираем запись
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM reminders WHERE account_id=$1 AND avito_chat_id=$2",
                    row["account_id"],
                    row["avito_chat_id"],
                )


def install(app) -> None:
    """Встраивает шедулер в жизненный цикл FastAPI (startup/shutdown)."""
    @app.on_event("startup")
    async def _start_scheduler():
        global _scheduler
        if _scheduler and _scheduler.running:
            return
        _scheduler = AsyncIOScheduler(timezone="UTC")
        _scheduler.start()
        _scheduler.add_job(
            remind_loop,
            trigger=IntervalTrigger(minutes=config.REMIND_AFTER_MIN),
            id="remind_loop",            # фиксируем ID
            replace_existing=True,       # не плодить дубли
            coalesce=True,               # сжать пропуски
            max_instances=1,             # не запускать параллельно
            misfire_grace_time=60,
        )
        log.info("Scheduler started (interval %s min)", config.REMIND_AFTER_MIN)

    @app.on_event("shutdown")
    async def _stop_scheduler():
        if _scheduler and _scheduler.running:
            _scheduler.shutdown(wait=False)
            log.info("Scheduler stopped")
