"""
Простейшая обёртка над Telegram Bot API.
"""
import logging, httpx
import config

log = logging.getLogger("AvitoNotify.telegram")


async def send_telegram(text: str) -> None:
    """
    Шлёт сообщение в Telegram и пишет статус в лог.
    Бросает исключение, если Telegram API вернул ошибку.
    """
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, data={"chat_id": config.TELEGRAM_ADMIN_USER_ID, "text": text})

    # Telegram вернул ошибку — пробрасываем как исключение
    if r.status_code != 200:
        log.error("Telegram error %s: %s", r.status_code, r.text)
        raise RuntimeError(f"Telegram API {r.status_code}: {r.text}")
    log.info("→ Telegram OK: %s", text[:80])



async def send_telegram_to(text: str, chat_id: int, bot_token: str | None = None) -> None:
    token = bot_token or getattr(config, "TELEGRAM_BOT_TOKEN", None)
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, data={"chat_id": chat_id, "text": text})
    if r.status_code != 200:
        log.error("Telegram error %s: %s", r.status_code, r.text)
        raise RuntimeError(f"Telegram API {r.status_code}: {r.text}")
    log.info("→ Telegram OK to %s: %s", chat_id, text[:80])