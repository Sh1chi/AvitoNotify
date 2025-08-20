"""
Bootstrap aiogram-бота в составе FastAPI-приложения.
"""
from __future__ import annotations
import asyncio
import logging
from aiogram import Bot, Dispatcher

import config
from .handlers_admin import router as admin_router
from .handlers_group import router as group_router
from .common import ensure_bot_record

log = logging.getLogger("AvitoNotify.aiogram")


def install(app) -> None:
    """
    Регистрирует запуск aiogram-бота вместе с FastAPI.
    """
    dp = Dispatcher()
    dp.include_router(admin_router)
    dp.include_router(group_router)

    @app.on_event("startup")
    async def _start():
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        await ensure_bot_record(bot)
        loop = asyncio.get_event_loop()
        app.state.aiogram_task = loop.create_task(dp.start_polling(bot))
        log.info("aiogram polling started")

    @app.on_event("shutdown")
    async def _stop():
        task = getattr(app.state, "aiogram_task", None)
        if task:
            task.cancel()
