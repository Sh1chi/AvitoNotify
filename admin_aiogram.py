# admin_aiogram.py — aiogram 3.x: админ-команды и онбординг
from __future__ import annotations
import asyncio, logging, re
from datetime import time as dtime
from typing import Optional

import asyncpg
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message, ChatMemberUpdated
from aiogram.filters import Command, CommandObject

import config
from db import get_pool

log = logging.getLogger("AvitoNotify.aiogram")

# --- Глобалы модуля ---
router = Router()
_pool: asyncpg.Pool | None = None
_bot_db_id: int | None = None


HELP_TEXT = (
    "👋 Админ-панель подключений.\n\n"
    "Команды:\n"
    "• /add_avito <avito_user_id> [name]\n"
    "• (в группе) /link <avito_user_id>\n"
    "• (в группе) /mute on|off\n"
    "• (в группе) /hours HH:MM-HH:MM [Europe/Moscow]\n"
    "• (в группе) /digest HH:MM|off\n"
)


async def _ensure_bot_record(bot: Bot) -> None:
    """Сохраняем/обновляем сведения о боте в notify.telegram_bots (tg_bot_id, username)."""
    global _bot_db_id
    me = await bot.get_me()
    tg_bot_id = int(me.id)
    username = me.username
    async with (await get_pool()).acquire() as conn:
        await conn.execute("""
            INSERT INTO notify.telegram_bots (tg_bot_id, username, is_active)
            VALUES ($1, $2, TRUE)
            ON CONFLICT (tg_bot_id) DO UPDATE
            SET username = EXCLUDED.username, is_active = TRUE;
        """, tg_bot_id, username)
        _bot_db_id = await conn.fetchval(
            "SELECT id FROM notify.telegram_bots WHERE tg_bot_id = $1",
            tg_bot_id
        )
    log.info("Bot @%s (tg_bot_id=%s) db_id=%s", username, tg_bot_id, _bot_db_id)


async def _upsert_chat(chat) -> int:
    """Сохраняем группу/чат в notify.telegram_chats, возвращаем его id."""
    tg_chat_id = int(chat.id)
    ctype = chat.type  # "group"/"supergroup"/"private"/"channel"
    title = getattr(chat, "title", None) or getattr(chat, "username", None) or str(tg_chat_id)
    async with (await get_pool()).acquire() as conn:
        await conn.execute("""
            INSERT INTO notify.telegram_chats (tg_chat_id, type, title)
            VALUES ($1, $2, $3)
            ON CONFLICT (tg_chat_id) DO UPDATE
            SET type = EXCLUDED.type, title = EXCLUDED.title;
        """, tg_chat_id, ctype, title)
        chat_db_id = await conn.fetchval(
            "SELECT id FROM notify.telegram_chats WHERE tg_chat_id = $1",
            tg_chat_id
        )
    return int(chat_db_id)


async def _ensure_account(avito_user_id: int, name: Optional[str]) -> int:
    async with (await get_pool()).acquire() as conn:
        await conn.execute("""
            INSERT INTO notify.accounts (avito_user_id, name)
            VALUES ($1, $2)
            ON CONFLICT (avito_user_id) DO UPDATE
            SET name = COALESCE(EXCLUDED.name, notify.accounts.name);
        """, avito_user_id, name)
        acc_id = await conn.fetchval(
            "SELECT id FROM notify.accounts WHERE avito_user_id = $1",
            avito_user_id
        )
    return int(acc_id)


async def _account_id_by_avito(avito_user_id: int) -> int | None:
    async with (await get_pool()).acquire() as conn:
        return await conn.fetchval("SELECT id FROM notify.accounts WHERE avito_user_id = $1", avito_user_id)


async def _ensure_link(account_id: int, chat_db_id: int) -> None:
    async with (await get_pool()).acquire() as conn:
        await conn.execute("""
            INSERT INTO notify.account_chat_links (account_id, chat_id, bot_id, muted)
            VALUES ($1, $2, $3, FALSE)
            ON CONFLICT (account_id, chat_id) DO NOTHING;
        """, account_id, chat_db_id, _bot_db_id)


async def _update_links_for_chat(chat_db_id: int, **kwargs) -> None:
    if not kwargs: return
    sets, vals = [], []
    for i, (k, v) in enumerate(kwargs.items(), start=1):
        sets.append(f"{k} = ${i}")
        vals.append(v)
    vals.append(chat_db_id)
    sql = f"UPDATE notify.account_chat_links SET {', '.join(sets)} WHERE chat_id = ${len(vals)}"
    async with (await get_pool()).acquire() as conn:
        await conn.execute(sql, *vals)


def _is_admin(message: Message) -> bool:
    user_id = int(message.from_user.id) if message.from_user else 0
    return user_id == int(config.TELEGRAM_ADMIN_USER_ID or 0)


def _parse_hours(s: str) -> tuple[dtime, dtime, Optional[str]]:
    m = re.match(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})(?:\s+([\w/\-]+))?$", s or "")
    if not m:
        raise ValueError("Формат: HH:MM-HH:MM [Europe/Moscow]")
    h1,m1,h2,m2,tz = int(m[1]),int(m[2]),int(m[3]),int(m[4]),m[5]
    if not (0<=h1<24 and 0<=h2<24 and 0<=m1<60 and 0<=m2<60):
        raise ValueError("Некорректное время")
    return dtime(h1,m1), dtime(h2,m2), tz

# --- Handlers ---

@router.message(Command("start", "help"))
async def cmd_help(message: Message):
    if message.chat.type != "private":
        return
    if not _is_admin(message):
        return
    await message.answer(HELP_TEXT)


@router.message(Command("add_avito"))
async def cmd_add_avito(message: Message, command: CommandObject):
    if message.chat.type != "private":
        return await message.answer("Эту команду нужно отправлять в личку боту.")
    if not _is_admin(message):
        return
    args = (command.args or "").strip().split()
    if not args:
        return await message.answer("Формат: /add_avito <avito_user_id> [name]")
    try:
        avito_user_id = int(args[0])
    except ValueError:
        return await message.answer("avito_user_id должен быть числом.")
    name = " ".join(args[1:]) if len(args) > 1 else None
    acc_id = await _ensure_account(avito_user_id, name)
    await message.answer(f"✅ Аккаунт добавлен (id={acc_id}, avito_user_id={avito_user_id}).")


@router.message(Command("link"))
async def cmd_link(message: Message, command: CommandObject):
    if message.chat.type not in ("group", "supergroup"):
        return await message.answer("Эту команду нужно отправлять в *группе*.")
    if not _is_admin(message):
        return
    args = (command.args or "").strip().split()
    if not args:
        return await message.answer("Формат: /link <avito_user_id>")
    try:
        avito_user_id = int(args[0])
    except ValueError:
        return await message.answer("avito_user_id должен быть числом.")
    chat_db_id = await _upsert_chat(message.chat)
    acc_id = await _account_id_by_avito(avito_user_id)
    if not acc_id:
        return await message.answer(f"Сначала добавьте аккаунт: /add_avito {avito_user_id}")
    await _ensure_link(acc_id, chat_db_id)
    await message.answer(f"🔗 Группа привязана к {avito_user_id}. Уведомления включены.")


@router.message(Command("mute"))
async def cmd_mute(message: Message, command: CommandObject):
    if message.chat.type not in ("group", "supergroup"):
        return
    if not _is_admin(message):
        return
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off"):
        return await message.answer("Формат: /mute on|off")
    muted = (arg == "on")
    chat_db_id = await _upsert_chat(message.chat)
    await _update_links_for_chat(chat_db_id, muted=muted)
    await message.answer("🔕 Отключены уведомления." if muted else "🔔 Включены уведомления.")


@router.message(Command("hours"))
async def cmd_hours(message: Message, command: CommandObject):
    if message.chat.type not in ("group", "supergroup"):
        return
    if not _is_admin(message):
        return
    args = (command.args or "").strip()
    if not args:
        return await message.answer("Формат: /hours HH:MM-HH:MM [Europe/Moscow]")
    try:
        start, end, tz = _parse_hours(args)
    except Exception as e:
        return await message.answer(f"Ошибка: {e}")
    chat_db_id = await _upsert_chat(message.chat)
    await _update_links_for_chat(chat_db_id, work_from=start, work_to=end, tz=tz)
    await message.answer(f"🕘 Рабочие часы: {start.strftime('%H:%M')}-{end.strftime('%H:%M')} {tz or ''}".strip())


@router.message(Command("digest"))
async def cmd_digest(message: Message, command: CommandObject):
    if message.chat.type not in ("group", "supergroup"):
        return
    if not _is_admin(message):
        return
    arg = (command.args or "").strip().lower()
    chat_db_id = await _upsert_chat(message.chat)
    if arg == "off":
        await _update_links_for_chat(chat_db_id, daily_digest_time=None)
        return await message.answer("🧹 Дайджест отключён.")
    m = re.match(r"^(\d{1,2}):(\d{2})$", arg or "")
    if not m:
        return await message.answer("Формат: /digest HH:MM|off")
    hh, mm = int(m[1]), int(m[2])
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return await message.answer("Неверное время.")
    await _update_links_for_chat(chat_db_id, daily_digest_time=dtime(hh, mm))
    await message.answer(f"🗞️ Дайджест в {hh:02d}:{mm:02d}.")


# Бота добавили/разрешили в группе
@router.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated, bot: Bot):
    chat = update.chat
    status = update.new_chat_member.status
    if chat.type in ("group", "supergroup") and status in ("member", "administrator"):
        await _upsert_chat(chat)
        await bot.send_message(chat.id, "✅ Бот добавлен. Введите /link <avito_user_id>.")


# --- Встраивание в FastAPI-приложение ---
def install(app) -> None:
    """Запуск aiogram poller-а как фоновой задачи FastAPI."""
    dp = Dispatcher()
    dp.include_router(router)

    @app.on_event("startup")
    async def _start():  # открыть пул БД
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        await _ensure_bot_record(bot)
        loop = asyncio.get_event_loop()
        app.state.aiogram_task = loop.create_task(dp.start_polling(bot))
        log.info("aiogram polling started")

    @app.on_event("shutdown")
    async def _stop():
        task = getattr(app.state, "aiogram_task", None)
        if task:
            task.cancel()
        if _pool:
            await _pool.close()  # type: ignore[func-returns-value]
