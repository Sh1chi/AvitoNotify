from __future__ import annotations
from typing import Optional
from db import get_pool
import telegram
import config

async def send_and_log(text: str, tg_chat_id: int) -> Optional[int]:
    """
    Отправляет сообщение в Telegram и сохраняет (tg_chat_id, message_id)
    для последующей очистки.
    """
    msg = await telegram.send_telegram_to(text, tg_chat_id)

    # msg — это dict из Telegram "result"
    message_id = None
    if isinstance(msg, dict):
        message_id = msg.get("message_id")
    else:
        # на всякий случай поддержим старый путь
        message_id = getattr(msg, "message_id", None)

    if message_id is None:
        return None

    async with (await get_pool()).acquire() as conn:
        await conn.execute(
            "INSERT INTO notify.sent_messages (tg_chat_id, tg_message_id) VALUES ($1, $2)",
            int(tg_chat_id), int(message_id),
        )
    return int(message_id)

async def cleanup_all_chats() -> int:
    """
    Удаляет все ещё не помеченные удалёнными сообщения бота во всех чатах.
    Возвращает количество помеченных удалёнными записей.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, tg_chat_id, tg_message_id FROM notify.sent_messages WHERE deleted_ts IS NULL"
        )
        ids: list[int] = []
        for r in rows:
            try:
                await telegram.delete_message(int(r["tg_chat_id"]), int(r["tg_message_id"]))
            except Exception:
                pass
            ids.append(int(r["id"]))
        if ids:
            await conn.execute(
                "UPDATE notify.sent_messages SET deleted_ts = now() WHERE id = ANY($1::bigint[])",
                ids,
            )

        # 🔻 ХАРД-УДАЛЕНИЕ старых «мягко удалённых» (ретенция)
        await conn.execute("""
                DELETE FROM notify.sent_messages
                WHERE deleted_ts IS NOT NULL
                  AND deleted_ts < now() - make_interval(days => $1)
            """, config.SENT_MESSAGES_RETENTION_DAYS)

        return len(ids)

async def cleanup_by_tg_chat(tg_chat_id: int) -> int:
    """
    Удаляет все ещё не удалённые сообщения бота в указанном tg-чате.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, tg_chat_id, tg_message_id
            FROM notify.sent_messages
            WHERE deleted_ts IS NULL AND tg_chat_id = $1
            """,
            int(tg_chat_id),
        )
        ids: list[int] = []
        for r in rows:
            try:
                await telegram.delete_message(int(r["tg_chat_id"]), int(r["tg_message_id"]))
            except Exception:
                pass
            ids.append(int(r["id"]))
        if ids:
            await conn.execute(
                "UPDATE notify.sent_messages SET deleted_ts = now() WHERE id = ANY($1::bigint[])",
                ids,
            )
        return len(ids)
