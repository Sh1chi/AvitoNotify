"""
Конфигурация проекта: переменные окружения, пути, логирование.
Загружается один раз и используется во всех частях проекта.
"""
import os, logging
from pathlib import Path
from dotenv import load_dotenv

# ── базовая конфигурация и валидация env ────────────────────────────────────
load_dotenv(".env")

# ── Обязательные параметры OAuth (если пусто — валидация в runtime) ────────
CLIENT_ID        = os.getenv("AVITO_CLIENT_ID")
CLIENT_SECRET    = os.getenv("AVITO_CLIENT_SECRET")
REDIRECT_URI     = os.getenv("AVITO_REDIRECT_URI")

# ── URL‑ы Avito API с дефолтами ────────────────────────────────────────────
TOKEN_URL        = os.getenv("AVITO_TOKEN_URL",  "https://api.avito.ru/token")
AVITO_API_BASE   = os.getenv("AVITO_API_BASE",  "https://api.avito.ru")
AVITO_HOOK_SECRET = os.getenv("AVITO_HOOK_SECRET", "changeme")
AVITO_USER_ID = int(os.getenv("AVITO_USER_ID", 0))

# ── Telegram bot credentials ───────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_ADMIN_USER_ID = int(os.getenv("TELEGRAM_ADMIN_USER_ID", "0") or "0")

# ── Настройки напоминаний ──────────────────────────────────────────────────
REMIND_AFTER_MIN = int(os.getenv("REMIND_AFTER_MIN", 1))  # интервал (в минутах)

# ── Файл для хранения access/refresh токенов Avito ────────────────────────
TOKENS_FILE = Path("tokens.json")

# ── Database ──────────────────────────────────────────────────────────────
NOTIFY_DB_URL = os.getenv(
    "NOTIFY_DB_URL",
    "postgresql://postgres:postgres@localhost:5432/avito?options=-csearch_path%3Dnotify",
)

# ── Настройка логгера, общий формат и уровень ─────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("avito_bridge")


AVITO_OWNER_USER_ID = int(os.getenv("AVITO_OWNER_USER_ID", "0") or "0")
AVITO_OAUTH_SCOPES = os.getenv(
    "AVITO_OAUTH_SCOPES",
    "user:read,messenger:read,messenger:write"
)