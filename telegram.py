"""
Простейшая обёртка над Telegram Bot API.
"""
import logging, httpx
import config

log = logging.getLogger("AvitoNotify.telegram")

def _one_line(s: str, limit: int = 160) -> str:
    s = " ".join((s or "").split())  # склеить строки и схлопнуть пробелы
    return (s[:limit] + "…") if len(s) > limit else s

async def send_telegram(text: str) -> None:
    """
    Шлёт сообщение в Telegram и пишет статус в лог.
    Бросает исключение, если Telegram API вернул ошибку.
    """
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, data={
            "chat_id": config.TELEGRAM_ADMIN_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        })
    if r.status_code != 200:
        log.error("Telegram error %s: %s", r.status_code, r.text)
        raise RuntimeError(f"Telegram API {r.status_code}: {r.text}")
    log.info("→ Telegram OK to admin: %s", _one_line(text))


async def send_telegram_to(text: str, chat_id: int, bot_token: str | None = None):
    """
    Шлёт сообщение в указанный чат и ВОЗВРАЩАЕТ объект result из Telegram
    (в нём лежит message_id). Парсим ошибку, чтобы наверняка.
    """
    token = bot_token or getattr(config, "TELEGRAM_BOT_TOKEN", None)
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        })

    if r.status_code != 200:
        log.error("Telegram error %s: %s", r.status_code, r.text)
        raise RuntimeError(f"Telegram API {r.status_code}: {r.text}")

    data = r.json()
    res = data.get("result")
    log.info("→ Telegram OK to admin: %s", _one_line(text))
    return res  # у res есть поле "message_id"


async def delete_message(chat_id: int, message_id: int, bot_token: str | None = None) -> None:
    """
    Удаляет сообщение по message_id (нужно для генуборки).
    """
    token = bot_token or getattr(config, "TELEGRAM_BOT_TOKEN", None)
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

    url = f"https://api.telegram.org/bot{token}/deleteMessage"
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, data={"chat_id": chat_id, "message_id": message_id})

    # Telegram на удаление часто отвечает 200, даже если сообщение уже удалено/устарело.
    # Логируем только явные HTTP-ошибки.
    if r.status_code != 200:
        log.warning("Telegram deleteMessage %s: %s", r.status_code, r.text)