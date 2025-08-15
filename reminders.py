"""
Отправка периодических напоминаний, если продавец не ответил клиенту.
"""
from datetime import datetime, timezone, timedelta
import logging, httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

import telegram, config
from auth import get_valid_access_token
from db import get_pool

log = logging.getLogger("AvitoNotify.reminders")
_scheduler: AsyncIOScheduler | None = None  # планировщик хранится глобально


async def _last_message_status(avito_user_id: int, avito_chat_id: int | str) -> str:
    """
    Возвращает 'buyer' | 'seller' | 'unknown', проверяя
    направление последнего сообщения в чате Avito.

    'buyer'  – последнее сообщение от клиента,
    'seller' – последнее сообщение от продавца,
    'unknown' – не удалось определить (ошибка сети или API).
    """
    chat_id = str(avito_chat_id)
    token = await get_valid_access_token(avito_user_id)
    url = f"{config.AVITO_API_BASE}/messenger/v2/accounts/{avito_user_id}/chats/{chat_id}"

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
    Основной цикл напоминаний:
    - получает из БД просроченные напоминания;
    - проверяет, кто написал последнее сообщение;
    - уведомляет или удаляет напоминание в зависимости от результата.
    """
    now = datetime.now(timezone.utc)
    sql_due = """
        SELECT r.account_id, a.avito_user_id, r.avito_chat_id, r.first_ts, r.last_reminder
        FROM   reminders r
        JOIN   accounts  a ON a.id = r.account_id
        WHERE  (r.last_reminder IS NULL OR $1 - r.last_reminder >= $2)
    """
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(sql_due, now, timedelta(minutes=config.REMIND_AFTER_MIN))

    for row in rows:
        status = await _last_message_status(row["avito_user_id"], row["avito_chat_id"])
        pool = await get_pool()

        if status == "buyer":
            minutes = int((now - row["first_ts"]).total_seconds() // 60)
            await _notify_linked_chats(
                row["account_id"],
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
            await _notify_linked_chats(
                row["account_id"],
                f"⚠️ Не удалось получить данные по чату #{row['avito_chat_id']}. Проверьте вручную."
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


async def _notify_linked_chats(account_id: int, text: str) -> None:
    """
    Отправляет текст во все TG-чаты, привязанные к аккаунту (muted=FALSE).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT tg_chat_id
            FROM v_account_chat_targets
            WHERE account_id = $1 AND muted = FALSE
        """, account_id)
    for r in rows:
        await telegram.send_telegram_to(text, r["tg_chat_id"])


def install(app) -> None:
    """
    Встраивает планировщик напоминаний в жизненный цикл FastAPI:
    - при старте приложения запускает APScheduler;
    - при остановке — корректно его останавливает.
    """
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
