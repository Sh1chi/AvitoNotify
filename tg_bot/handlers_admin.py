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



@router.message(F.chat.type == "private", Command("start", "help", ignore_mention=True))
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
