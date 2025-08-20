"""
Общие утилиты бота: БД-хелперы, проверка админа, парсинг времени, запись бота.
"""
from __future__ import annotations
import logging
import re
from datetime import time as dtime
from typing import Optional

from aiogram import Bot
from aiogram.types import Message

import config
from db import get_pool

log = logging.getLogger("AvitoNotify.aiogram.common")

_BOT_DB_ID: Optional[int] = None


def is_admin_message(message: Message) -> bool:
    user_id = int(message.from_user.id) if message.from_user else 0
    admin_id = int(getattr(config, "TELEGRAM_ADMIN_USER_ID", 0) or 0)
    return user_id == admin_id


async def ensure_bot_record(bot: Bot) -> int:
    """
    Создаёт/обновляет запись бота в notify.telegram_bots и сохраняет bot_db_id.
    """
    global _BOT_DB_ID
    me = await bot.get_me()
    tg_bot_id = int(me.id)
    username = me.username or ""
    async with (await get_pool()).acquire() as conn:
        await conn.execute(
            """
            INSERT INTO notify.telegram_bots (tg_bot_id, username, is_active)
            VALUES ($1, $2, TRUE)
            ON CONFLICT (tg_bot_id) DO UPDATE
            SET username = EXCLUDED.username, is_active = TRUE;
            """,
            tg_bot_id,
            username,
        )
        _BOT_DB_ID = await conn.fetchval(
            "SELECT id FROM notify.telegram_bots WHERE tg_bot_id = $1",
            tg_bot_id,
        )
    log.info("Bot @%s (tg_bot_id=%s) db_id=%s", username, tg_bot_id, _BOT_DB_ID)
    return int(_BOT_DB_ID)


async def upsert_chat_and_get_id(chat) -> int:
    """
    Создаёт/обновляет запись чата и возвращает его внутренний id (notify.telegram_chats.id).
    """
    tg_chat_id = int(chat.id)
    ctype = chat.type  # "group"/"supergroup"/"private"/"channel"
    title = getattr(chat, "title", None) or getattr(chat, "username", None) or str(tg_chat_id)
    async with (await get_pool()).acquire() as conn:
        await conn.execute(
            """
            INSERT INTO notify.telegram_chats (tg_chat_id, type, title)
            VALUES ($1, $2, $3)
            ON CONFLICT (tg_chat_id) DO UPDATE
            SET type = EXCLUDED.type, title = EXCLUDED.title;
            """,
            tg_chat_id,
            ctype,
            title,
        )
        chat_db_id = await conn.fetchval(
            "SELECT id FROM notify.telegram_chats WHERE tg_chat_id = $1",
            tg_chat_id,
        )
    return int(chat_db_id)


async def account_id_by_avito(avito_user_id: int) -> Optional[int]:
    """
    Возвращает внутренний id аккаунта (notify.accounts.id) по avito_user_id.
    """
    async with (await get_pool()).acquire() as conn:
        return await conn.fetchval(
            "SELECT id FROM notify.accounts WHERE avito_user_id = $1",
            int(avito_user_id),
        )


async def ensure_link(account_id: int, chat_db_id: int) -> None:
    """
    Создаёт связь аккаунта с чатом, если её ещё нет.
    """
    async with (await get_pool()).acquire() as conn:
        await conn.execute(
            """
            INSERT INTO notify.account_chat_links (account_id, chat_id, bot_id, muted)
            VALUES ($1, $2, $3, FALSE)
            ON CONFLICT (account_id, chat_id) DO NOTHING;
            """,
            account_id,
            chat_db_id,
            _BOT_DB_ID,
        )

        await conn.execute(
            """
            UPDATE notify.accounts
            SET display_name = COALESCE(display_name, name)
            WHERE id = $1
            """,
            account_id,
        )


async def update_links_for_chat(chat_db_id: int, **kwargs) -> None:
    """
    Массовое обновление настроек связей всех аккаунтов с данным чатом.
    Примеры полей: muted, work_from, work_to, tz, daily_digest_time.
    """
    if not kwargs:
        return
    sets = []
    vals = []
    for i, (k, v) in enumerate(kwargs.items(), start=1):
        sets.append(f"{k} = ${i}")
        vals.append(v)
    vals.append(chat_db_id)
    sql = f"UPDATE notify.account_chat_links SET {', '.join(sets)} WHERE chat_id = ${len(vals)}"
    async with (await get_pool()).acquire() as conn:
        await conn.execute(sql, *vals)


def parse_hours(s: str) -> tuple[dtime, dtime, Optional[str]]:
    """
    Разбирает строку формата 'HH:MM-HH:MM [Europe/Moscow]' в (start, end, tz).
    """
    m = re.match(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})(?:\s+([\w/\-]+))?$", s or "")
    if not m:
        raise ValueError("Формат: HH:MM-HH:MM [Europe/Moscow]")
    h1, m1, h2, m2, tz = int(m[1]), int(m[2]), int(m[3]), int(m[4]), m[5]
    if not (0 <= h1 < 24 and 0 <= h2 < 24 and 0 <= m1 < 60 and 0 <= m2 < 60):
        raise ValueError("Некорректное время")
    return dtime(h1, m1), dtime(h2, m2), tz
