
import os
import logging
import asyncio
from dataclasses import dataclass
from typing import Optional

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN: str = os.environ["BOT_TOKEN"]       # KeyError якщо не задано — краще ніж None
CHAT_ID: str   = os.environ["CHAT_ID"]

ALERTS_API_URL  = "https://alerts.com.ua/api/states"
POLL_INTERVAL   = 30        # секунди між запитами
REQUEST_TIMEOUT = 10        # секунди на HTTP-запит
MAX_RETRIES     = 3         # кількість спроб при помилці API
RETRY_DELAY     = 5         # секунди між повторами

# ---------------------------------------------------------------------------
# Типи
# ---------------------------------------------------------------------------

@dataclass(frozen=True, eq=True)
class RegionState:
    status: bool
    alert_type: str

ICONS: dict[str, str] = {
    "AIR":         "🛩️",
    "ARTILLERY":   "💣",
    "DRONE":       "🛰️",
    "URBAN_FIGHT": "⚔️",
    "UNKNOWN":     "🚨",
}

# ---------------------------------------------------------------------------
# Стан
# ---------------------------------------------------------------------------

last_status: dict[str, RegionState] = {}

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

async def fetch_regions(client: httpx.AsyncClient) -> Optional[list[dict]]:
    """Отримує дані тривог з API. Повертає None при невдачі."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = await client.get(ALERTS_API_URL, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.warning("HTTP %s від API (спроба %d/%d)", e.response.status_code, attempt, MAX_RETRIES)
        except httpx.RequestError as e:
            logger.warning("Помилка запиту до API: %s (спроба %d/%d)", e, attempt, MAX_RETRIES)
        except Exception as e:
            logger.error("Несподівана помилка: %s (спроба %d/%d)", e, attempt, MAX_RETRIES)

        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_DELAY)

    logger.error("API недоступний після %d спроб", MAX_RETRIES)
    return None

# ---------------------------------------------------------------------------
# Логіка тривог
# ---------------------------------------------------------------------------

def build_message(active: list[str]) -> str:
    if active:
        return "<b>🚨 Повітряна тривога:</b>\n\n" + "\n".join(active)
    return "✅ <b>Відбій тривоги по всій Україні</b>"


async def check_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Перевіряє тривоги та надсилає повідомлення при змінах."""
    global last_status

    client: httpx.AsyncClient = context.bot_data["http_client"]
    regions = await fetch_regions(client)
    if regions is None:
        return

    active: list[str] = []
    changed = False

    for region in regions:
        name: str        = region.get("name", "")
        status: bool     = bool(region.get("alert", False))
        alert_type: str  = region.get("type", "UNKNOWN")

        if not name:
            continue

        current = RegionState(status=status, alert_type=alert_type)

        if last_status.get(name) != current:
            changed = True
            last_status[name] = current

        if status:
            icon = ICONS.get(alert_type, "🚨")
            active.append(f"{icon} <b>{name}</b>")

    if changed:
        msg = build_message(active)
        try:
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=msg,
                parse_mode=ParseMode.HTML,
            )
            logger.info("Надіслано оновлення: %d активних регіонів", len(active))
        except Exception as e:
            logger.error("Не вдалося надіслати повідомлення: %s", e)

# ---------------------------------------------------------------------------
# Команди
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Бот тривог запущено 🚨")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показує поточний стан тривог на запит."""
    if not last_status:
        await update.message.reply_text("Дані ще не завантажені, зачекайте...")
        return

    active = [
        f"{ICONS.get(s.alert_type, '🚨')} <b>{name}</b>"
        for name, s in last_status.items()
        if s.status
    ]
    msg = build_message(active)
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def post_init(application) -> None:
    """Ініціалізація після старту — створюємо HTTP-клієнт та запускаємо JobQueue."""
    application.bot_data["http_client"] = httpx.AsyncClient()

    # JobQueue замість ручного asyncio.create_task — вбудований механізм PTB
    application.job_queue.run_repeating(
        check_alerts,
        interval=POLL_INTERVAL,
        first=0,            # перший запит одразу після старту
        name="alerts_loop",
    )
    logger.info("Бот запущено. Інтервал перевірки: %ds", POLL_INTERVAL)


async def post_shutdown(application) -> None:
    """Закриває HTTP-клієнт при зупинці."""
    client: httpx.AsyncClient = application.bot_data.get("http_client")
    if client:
        await client.aclose()
        logger.info("HTTP-клієнт закрито")

# ---------------------------------------------------------------------------
# Точка входу
# ---------------------------------------------------------------------------

def main() -> None:
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("status", cmd_status))

    application.run_polling()


if __name__ == "__main__":
    main()
