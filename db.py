"""
asyncpg-pool для таблицы notify.reminders
"""
import asyncpg
from fastapi import FastAPI
from config import NOTIFY_DB_URL
import logging

log = logging.getLogger(__name__)
pool: asyncpg.Pool | None = None


def install_pool(app: FastAPI) -> None:
    @app.on_event("startup")
    async def _open() -> None:
        global pool
        pool = await asyncpg.create_pool(NOTIFY_DB_URL, min_size=1, max_size=5)
        log.info("Notifier DB pool ready")

    @app.on_event("shutdown")
    async def _close() -> None:
        if pool:
            await pool.close()


async def get_pool() -> asyncpg.Pool:
    if pool is None:           # защита от раннего вызова
        raise RuntimeError("DB pool not initialised")
    return pool
