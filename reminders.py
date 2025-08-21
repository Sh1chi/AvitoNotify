"""
Отправка периодических напоминаний, если продавец не ответил клиенту.
"""
from datetime import datetime, timezone, timedelta, time as dtime
from zoneinfo import ZoneInfo
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
        SELECT r.account_id, a.avito_user_id, a.name, r.avito_chat_id, r.first_ts, r.last_reminder
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
            sent = await _notify_linked_chats_in_hours(
                row["account_id"],
                f"⏰ Уже {minutes} мин без ответа в чате #{row['avito_chat_id']}",
                now
            )
            if sent:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE reminders SET last_reminder = $1 WHERE account_id=$2 AND avito_chat_id=$3",
                        now, row["account_id"], row["avito_chat_id"],
                    )


        elif status == "unknown":
            account_label = row["name"] or row["avito_user_id"]
            await telegram.send_telegram(
                f"⚠️ Не удалось получить данные по чату #{row['avito_chat_id']} "
                f"(аккаунт {account_label}, {row['avito_user_id']}). Проверьте вручную."
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

        # Цикл напоминаний
        _scheduler.add_job(
            remind_loop,
            trigger=IntervalTrigger(minutes=config.REMIND_AFTER_MIN),
            id="remind_loop",            # фиксируем ID
            replace_existing=True,       # не плодить дубли
            coalesce=True,               # сжать пропуски
            max_instances=1,             # не запускать параллельно
            misfire_grace_time=60,
        )

        # Утренний дайджест
        _scheduler.add_job(
            morning_digest_tick,
            trigger=IntervalTrigger(minutes=1),
            id="morning_digest",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

        log.info("Scheduler started (interval %s min)", config.REMIND_AFTER_MIN)

    @app.on_event("shutdown")
    async def _stop_scheduler():
        if _scheduler and _scheduler.running:
            _scheduler.shutdown(wait=False)
            log.info("Scheduler stopped")


#-----------------------Реализация рабочего времени-------------------------
async def _iter_links(account_id: int) -> list[dict]:
    """
    Возвращает настройки всех связок аккаунта с чатами:
    tg_chat_id, muted, work_from, work_to, tz, daily_digest_time.
    """
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch("""
            SELECT ch.tg_chat_id, l.muted, l.work_from, l.work_to, l.tz
            FROM notify.account_chat_links l
            JOIN notify.telegram_chats ch ON ch.id = l.chat_id
            WHERE l.account_id = $1
        """, account_id)
    return [dict(r) for r in rows]


def _within_hours(now_utc: datetime, start: dtime|None, end: dtime|None, tz: str|None) -> bool:
    """
    Проверяет, укладывается ли локальное время в окно [start, end),
    поддерживает окна через полночь.
    """
    if not start or not end:
        return True
    local = now_utc.astimezone(ZoneInfo(tz or "UTC")).timetz().replace(tzinfo=None)
    if start <= end:
        return start <= local < end
    return local >= start or local < end  # окно через полночь


async def _notify_linked_chats_in_hours(account_id: int, text: str, now_utc: datetime) -> int:
    """
    Отправляет текст только в те чаты аккаунта, где сейчас «рабочее» время.
    """
    sent = 0
    for link in await _iter_links(account_id):
        if link["muted"]:
            continue
        if _within_hours(now_utc, link["work_from"], link["work_to"], link["tz"]):
            await telegram.send_telegram_to(text, link["tg_chat_id"])
            sent += 1
    return sent


async def _send_digest_for_link(link: dict, now_utc: datetime) -> None:
    """
    Формирует и отправляет дайджест по активным напоминаниям аккаунта в конкретный чат.
    """
    if link.get("muted"):
        return

    pool = await get_pool()
    async with pool.acquire() as conn:
        rems = await conn.fetch("""
            SELECT avito_chat_id, first_ts
            FROM notify.reminders
            WHERE account_id = $1
            ORDER BY first_ts
        """, link["account_id"])
        if not rems:
            return

        account = await conn.fetchrow(
            "SELECT name, avito_user_id FROM notify.accounts WHERE id = $1",
            link["account_id"]
        )

    title = account["name"] or str(account["avito_user_id"])
    lines = [f"🗞️ Утренний отчёт по аккаунту {title} ({now_utc.date().isoformat()})"]
    for r in rems:
        minutes = int((now_utc - r["first_ts"]).total_seconds() // 60)
        lines.append(f"• Чат #{r['avito_chat_id']}: {minutes} мин без ответа")
    await telegram.send_telegram_to("\n".join(lines), link["tg_chat_id"])


async def morning_digest_tick() -> None:
    """
    Проверяет, у каких связок «наступила» их daily_digest_time в локальной TZ, и шлёт дайджест.
    """
    now_utc = datetime.now(timezone.utc)
    async with (await get_pool()).acquire() as conn:
        links = await conn.fetch("""
            SELECT l.account_id, ch.tg_chat_id, l.daily_digest_time, l.tz, l.muted
            FROM notify.account_chat_links l
            JOIN notify.telegram_chats ch ON ch.id = l.chat_id
            WHERE l.daily_digest_time IS NOT NULL
        """)
    for l in links:
        if l["muted"]:
            continue
        tz = ZoneInfo(l["tz"] or "UTC")
        local = now_utc.astimezone(tz).timetz().replace(second=0, microsecond=0)
        if local == l["daily_digest_time"]:
            await _send_digest_for_link(dict(l), now_utc)