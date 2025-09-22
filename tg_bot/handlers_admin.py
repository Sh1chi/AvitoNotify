"""
Команды администратора в личке: /help, /howto, /avito_link, /delete_account.
"""
from __future__ import annotations
import asyncio
import logging
import re
from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from db import get_pool
import auth

from .common import is_admin_message
from .texts import HELP_TEXT_ADMIN_PRIVATE, HELP_TEXT_GROUP_PUBLIC, HOWTO_TEXT

log = logging.getLogger("AvitoNotify.aiogram.admin")
router = Router()


@router.message(F.chat.type == "private", Command( "help", ignore_mention=True))
async def cmd_help_private(message: Message):
    """
    В ЛС: админу — полный справочник, остальным — публичная справка.
    """
    if is_admin_message(message):
        return await message.answer(HELP_TEXT_ADMIN_PRIVATE)
    return await message.answer(HELP_TEXT_GROUP_PUBLIC)


@router.message(Command("howto"))
async def cmd_howto(message: Message):
    """
    Короткая инструкция для админа по подключению Avito.
    """
    if message.chat.type != "private":
        return
    if not is_admin_message(message):
        return
    await message.answer(HOWTO_TEXT)


@router.message(Command("avito_link"))
async def cmd_avito_link(message: Message):
    """
    Возвращает OAuth-ссылку для подключения нового Avito-аккаунта.
    """
    if message.chat.type != "private":
        return
    if not is_admin_message(message):
        return
    url = auth.build_authorize_url()
    await message.answer(f"Ссылка авторизации Avito:\n{url}")


@router.message(Command("delete_account"))
async def cmd_delete_account(message: Message, command: CommandObject):
    """
    Полное удаление аккаунта (БД+связи+напоминания+токены).
    Формат: /delete_account <avito_user_id>
    """
    if message.chat.type != "private":
        return
    if not is_admin_message(message):
        return

    args = (command.args or "").strip()
    if not args or not re.fullmatch(r"\d+", args):
        return await message.answer("Формат: /delete_account <avito_user_id>")

    avito_user_id = int(args)
    async with (await get_pool()).acquire() as conn:
        acc_id = await conn.fetchval(
            "SELECT id FROM notify.accounts WHERE avito_user_id=$1",
            avito_user_id,
        )
        if not acc_id:
            return await message.answer("Аккаунт с таким avito_user_id не найден в БД.")
        async with conn.transaction():
            await conn.execute("DELETE FROM notify.reminders WHERE account_id=$1", acc_id)
            await conn.execute("DELETE FROM notify.account_chat_links WHERE account_id=$1", acc_id)
            await conn.execute("DELETE FROM notify.accounts WHERE id=$1", acc_id)

    # Очистка токенов (поддержка sync/async реализаций)
    if hasattr(auth, "delete_tokens_for_user"):
        try:
            func = auth.delete_tokens_for_user  # type: ignore[attr-defined]
            if asyncio.iscoroutinefunction(func):
                await func(avito_user_id)  # type: ignore[misc]
            else:
                func(avito_user_id)  # type: ignore[misc,call-arg]
        except Exception as e:
            log.warning("Ошибка очистки токенов пользователя %s: %s", avito_user_id, e)

    await message.answer("🗑 Аккаунт удалён, связи и напоминания очищены.")


@router.message(Command("summary"))
async def cmd_summary(message: Message):
    # один главный админ в ЛС
    if message.chat.type != "private" or not is_admin_message(message):
        return

    async with (await get_pool()).acquire() as conn:
        accounts = await conn.fetch(
            "SELECT avito_user_id, COALESCE(display_name, name, '') AS name "
            "FROM notify.accounts ORDER BY avito_user_id"
        )
        chats = await conn.fetch(
            "SELECT COALESCE(title,'') AS title, tg_chat_id "
            "FROM notify.telegram_chats ORDER BY id"
        )
        links = await conn.fetch(
            """
            SELECT
                a.avito_user_id,
                COALESCE(a.display_name, a.name, '') AS acc_name,
                COALESCE(c.title,'') AS chat_title,
                l.muted,
                l.work_from,
                l.work_to,
                l.tz,
                l.daily_digest_time
            FROM notify.account_chat_links l
            JOIN notify.accounts a ON a.id = l.account_id
            JOIN notify.telegram_chats c ON c.id = l.chat_id
            ORDER BY a.avito_user_id, c.id
            """
        )

    def fmt_hours(start, end, tz):
        if start and end and start.hour == end.hour and start.minute == end.minute:
            return f"24/7" + (f" ({tz})" if tz else "")
        if start and end:
            s = f"{start:%H:%M}–{end:%H:%M}"
            return s + (f" ({tz})" if tz else "")
        return "нет"

    def fmt_time(val):
        return f"{val:%H:%M}" if val else "нет"

    parts = []
    parts.append("📊 Сводка")

    # Аккаунты — только user id и имя (если есть)
    parts.append("\n\n👤 Аккаунты:")
    if accounts:
        for a in accounts:
            parts.append(f"• {a['avito_user_id']}" + (f" — {a['name']}" if a['name'] else ""))
    else:
        parts.append("• нет")

    # Чаты — без внутренних id и типов
    parts.append("\n\n💬 Чаты:")
    if chats:
        for c in chats:
            title = c["title"] or str(c["tg_chat_id"])
            parts.append(f"• {title}")
    else:
        parts.append("• нет")

    # Привязки — «человечески»
    parts.append("\n\n🔗 Привязки:")
    if links:
        for l in links:
            acc_label = (f"{l['acc_name']} " if l["acc_name"] else "") + f"({l['avito_user_id']})"
            chat_title = l["chat_title"] or "Без названия"
            parts.append(f"• Аккаунт {acc_label} подключён к чату «{chat_title}»")
            parts.append(f"   ▸ Уведомления: {'включены' if not l['muted'] else 'выключены'}")
            parts.append(f"   ▸ Рабочее время: {fmt_hours(l['work_from'], l['work_to'], l['tz'])}")
            parts.append(f"   ▸ Утренний дайджест: {fmt_time(l['daily_digest_time'])}")
            parts.append("")
    else:
        parts.append("• нет")

    text = "\n".join(parts).rstrip()
    for i in range(0, len(text), 3500):
        await message.answer(text[i:i + 3500])


@router.message(Command("set_name"))
async def cmd_set_name(message: Message):
    if message.chat.type != "private" or not is_admin_message(message):
        return
    args = (message.text or "").split(maxsplit=2)
    # ожидаем: /set_name <id> <имя>
    if len(args) < 3 or not args[1].isdigit():
        return await message.answer("Формат: /set_name <avito_user_id> <новое имя>")
    avito_user_id = int(args[1])
    new_name = args[2].strip()
    if not new_name:
        return await message.answer("Укажите новое имя.")
    async with (await get_pool()).acquire() as conn:
        updated = await conn.execute(
            "UPDATE notify.accounts SET display_name=$2 WHERE avito_user_id=$1",
            avito_user_id, new_name
        )
    await message.answer(f"✅ Имя для аккаунта {avito_user_id} обновлено: {new_name}")


@router.message(Command("clear_reminders"))
async def cmd_clear_reminders(message: Message):
    """
    Удалить все напоминания для заданного avito_user_id.
    Доступно только админу в ЛС: /clear_reminders <avito_user_id>
    """
    if message.chat.type != "private":
        return
    if not is_admin_message(message):
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().isdigit():
        return await message.answer("Формат: /clear_reminders <avito_user_id>")

    avito_user_id = int(args[1].strip())
    async with (await get_pool()).acquire() as conn:
        status = await conn.execute(
            """
            DELETE FROM notify.reminders r
            USING notify.accounts a
            WHERE a.id = r.account_id
              AND a.avito_user_id = $1
            """,
            avito_user_id,
        )

    deleted = 0
    if isinstance(status, str) and status.startswith("DELETE"):
        try:
            deleted = int(status.split()[-1])
        except Exception:
            deleted = 0

    if deleted == 0:
        return await message.answer(f"Напоминаний для аккаунта {avito_user_id} не найдено.")
    return await message.answer(f"🧹 Удалено напоминаний: {deleted} (аккаунт {avito_user_id}).")


@router.message(Command("cleanup_now"))
async def cmd_cleanup_now(message: Message):
    if not is_admin_message(message):
        return
    from notifications import cleanup_all_chats
    n = await cleanup_all_chats()
    await message.answer(f"🧹 Удалено сообщений: {n}")