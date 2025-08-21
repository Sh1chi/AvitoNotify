"""
Команды для групп/супергрупп: /help, /link, /unlink, /mute, /hours, /digest.
"""
from __future__ import annotations
import logging
import re
from datetime import time as dtime
from aiogram import Router, Bot, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, ChatMemberUpdated
from aiogram.enums import ChatMemberStatus


from .common import (
    upsert_chat_and_get_id,
    account_id_by_avito,
    ensure_link,
    update_links_for_chat,
    parse_hours,
)
from .texts import HELP_TEXT_GROUP_ADMIN

log = logging.getLogger("AvitoNotify.aiogram.group")
router = Router()


async def is_chat_admin(message: Message) -> bool:
    member = await message.chat.get_member(message.from_user.id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)


@router.message(F.chat.type.in_({"group", "supergroup"}), Command( "help", ignore_mention=True))
async def cmd_help_group(message: Message):
    """
    В группе: администратору — расширенный набор, остальным — публичная справка.
    """
    if await is_chat_admin(message):
        return await message.answer(HELP_TEXT_GROUP_ADMIN)
    return await message.answer("Только администратор чата может выполнять эту команду.")


@router.message(Command("link"))
async def cmd_link(message: Message, command: CommandObject):
    """
    Привязывает текущую группу к существующему Avito-аккаунту.
    Формат: /link <avito_user_id>
    """
    if message.chat.type not in ("group", "supergroup"):
        return
    if not await is_chat_admin(message):
        return await message.answer("Только администратор чата может выполнять эту команду.")
    arg = (command.args or "").strip()
    if not arg or not re.fullmatch(r"\d+", arg):
        return await message.answer("Формат: /link <avito_user_id>")

    avito_user_id = int(arg)
    chat_db_id = await upsert_chat_and_get_id(message.chat)
    acc_id = await account_id_by_avito(avito_user_id)
    if not acc_id:
        return await message.answer(
            "Сначала подключите аккаунт через /avito_link в ЛС админа (пройдите OAuth)."
        )
    await ensure_link(acc_id, chat_db_id)

    # показать кастомное имя (display_name) вместо id
    from db import get_pool  # если ещё не импортирован вверху
    async with (await get_pool()).acquire() as conn:
        label = await conn.fetchval(
            "SELECT COALESCE(display_name, name, avito_user_id::text) FROM notify.accounts WHERE id=$1",
            acc_id,
        )
    await message.answer(f"🔗 Группа привязана к {label}. Уведомления включены.")


@router.message(Command("unlink"))
async def cmd_unlink(message: Message, command: CommandObject):
    """
    Отвязывает аккаунт от текущего чата.
    Формат: /unlink <avito_user_id>
    """
    if message.chat.type not in ("group", "supergroup"):
        return
    if not await is_chat_admin(message):
        return await message.answer("Только администратор чата может выполнять эту команду.")
    arg = (command.args or "").strip()
    if not arg or not re.fullmatch(r"\d+", arg):
        return await message.answer("Формат: /unlink <avito_user_id>")

    avito_user_id = int(arg)
    acc_id = await account_id_by_avito(avito_user_id)
    if not acc_id:
        return await message.answer("Аккаунт с таким avito_user_id не найден в БД.")
    chat_db_id = await upsert_chat_and_get_id(message.chat)
    from db import get_pool  # локальный импорт, чтобы не тянуть в общий модуль
    async with (await get_pool()).acquire() as conn:
        await conn.execute(
            "DELETE FROM notify.account_chat_links WHERE account_id=$1 AND chat_id=$2",
            acc_id,
            chat_db_id,
        )
    await message.answer("🔓 Связь аккаунта и текущего чата удалена.")


@router.message(Command("mute"))
async def cmd_mute(message: Message, command: CommandObject):
    """
    Включает/выключает уведомления в текущей группе.
    Формат: /mute on|off
    """
    if message.chat.type not in ("group", "supergroup"):
        return
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off"):
        return await message.answer("Формат: /mute on|off")
    muted = (arg == "on")
    chat_db_id = await upsert_chat_and_get_id(message.chat)
    await update_links_for_chat(chat_db_id, muted=muted)
    await message.answer("🔕 Отключены уведомления." if muted else "🔔 Включены уведомления.")


@router.message(Command("hours"))
async def cmd_hours(message: Message, command: CommandObject):
    """
    Задаёт рабочие часы в текущей группе.
    Формат: /hours HH:MM-HH:MM [Europe/Moscow]
    """
    if message.chat.type not in ("group", "supergroup"):
        return
    if not await is_chat_admin(message):
        return await message.answer("Только администратор чата может выполнять эту команду.")
    args = (command.args or "").strip()
    if not args:
        return await message.answer("Формат: /hours HH:MM-HH:MM [Europe/Moscow]")
    try:
        start, end, tz = parse_hours(args)
    except Exception as e:
        return await message.answer(f"Ошибка: {e}")
    chat_db_id = await upsert_chat_and_get_id(message.chat)
    await update_links_for_chat(chat_db_id, work_from=start, work_to=end, tz=tz)
    tz_suffix = f" {tz}" if tz else ""
    await message.answer(
        f"🕘 Рабочие часы: {start.strftime('%H:%M')}-{end.strftime('%H:%M')}{tz_suffix}"
    )


@router.message(Command("digest"))
async def cmd_digest(message: Message, command: CommandObject):
    """
    Настраивает ежедневный дайджест или отключает его.
    Формат: /digest HH:MM|off
    """
    if message.chat.type not in ("group", "supergroup"):
        return
    if not await is_chat_admin(message):
        return await message.answer("Только администратор чата может выполнять эту команду.")
    arg = (command.args or "").strip().lower()
    chat_db_id = await upsert_chat_and_get_id(message.chat)
    if arg == "off":
        await update_links_for_chat(chat_db_id, daily_digest_time=None)
        return await message.answer("🧹 Дайджест отключён.")
    m = re.match(r"^(\d{1,2}):(\d{2})$", arg or "")
    if not m:
        return await message.answer("Формат: /digest HH:MM|off")
    hh, mm = int(m[1]), int(m[2])
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return await message.answer("Неверное время.")
    await update_links_for_chat(chat_db_id, daily_digest_time=dtime(hh, mm))
    await message.answer(f"🗞️ Дайджест в {hh:02d}:{mm:02d}.")


@router.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated, bot: Bot):
    """
    Реакция на изменение статуса бота в группе:
    - при добавлении: регистрируем чат и показываем подсказку;
    - при удалении: чистим связи и удаляем чат из БД.
    """
    chat = update.chat
    status = update.new_chat_member.status

    if chat.type not in ("group", "supergroup"):
        return

    # Бота добавили / сделали админом
    if status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        await upsert_chat_and_get_id(chat)
        return await bot.send_message(chat.id, "✅ Бот добавлен. Введите /link <avito_user_id>.")

    # Бота удалили/кикнули/он вышел — чистим БД
    if status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        chat_db_id = await upsert_chat_and_get_id(chat)  # гарантируем, что знаем id
        from db import get_pool
        async with (await get_pool()).acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM notify.account_chat_links WHERE chat_id=$1",
                    chat_db_id,
                )
                await conn.execute(
                    "DELETE FROM notify.telegram_chats WHERE id=$1",
                    chat_db_id,
                )
        log.info("Удалён чат %s и все его связи (bot removed).", chat.id)
