"""
Точка входа: собирает FastAPI-приложение,
подключает роутеры и стартует планировщик.
"""
import logging
from fastapi import FastAPI, Request

import reminders
from routes import public, webhook
from db import install_pool
from admin_aiogram import install as install_aiogram
from reminders import install as install_reminders

log = logging.getLogger("avito_bridge.main")

app = FastAPI(title="Avito OAuth bridge")
install_pool(app)
install_aiogram(app)
install_reminders(app)

# Подключаем маршруты
app.include_router(public.router)
app.include_router(webhook.router)


@app.middleware("http")
async def _log_requests(request: Request, call_next):
    """
    Middleware для логирования всех входящих HTTP-запросов.
    """
    log.info("--> %s %s", request.method, request.url.path)
    resp = await call_next(request)
    log.info("<-- %s %s", resp.status_code, request.url.path)
    return resp


@app.on_event("startup")
async def _startup():
    """
    Инициализация приложения при запуске (регистрация планировщика).
    """
    log.info("Startup complete")
