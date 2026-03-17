import os
import logging
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
CHAT_ID: str   = os.environ["CHAT_ID"]

ALERTS_API_URL  = "https://ubilling.net.ua/aerialalerts/"
POLL_INTERVAL   = 5
REQUEST_TIMEOUT = 10
MAX_RETRIES     = 3
RETRY_DELAY     = 5

# Якщо нові тривоги прийшли протягом N секунд після попередніх — це одна хвиля
WAVE_WINDOW_SEC = 15

# ---------------------------------------------------------------------------
# Типи
# ---------------------------------------------------------------------------

@dataclass
class AlertWave:
    """Одна хвиля тривог — одне повідомлення в чаті."""
    regions: set[str]                        # області цієї хвилі
    message_id: Optional[int] = None         # ID повідомлення в Telegram
    cleared: dict[str, str] = field(default_factory=dict)  # { region: time }
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

# ---------------------------------------------------------------------------
# Стан
# ---------------------------------------------------------------------------

# Поточні активні хвилі: остання активна хвиля
current_wave: Optional[AlertWave] = None

# Всі активні регіони прямо зараз
active_regions: set[str] = set()

# Перший запуск
is_first_run: bool = True

# ---------------------------------------------------------------------------
# Утіліти
# ---------------------------------------------------------------------------

def now_kyiv() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%H:%M")


def build_wave_message(wave: AlertWave) -> str:
    lines = ["<b>🚨 Повітряна тривога:</b>\n"]

    for name in sorted(wave.regions):
        if name in wave.cleared:
            lines.append(f"✅ <s>{name}</s>")
        else:
            lines.append(f"🚨 {name}")

    if wave.cleared:
        lines.append("")
        for name, t in sorted(wave.cleared.items(), key=lambda x: x[1]):
            lines.append(f"✅ <b>UPD {t}:</b> відбій — {name}")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

async def fetch_alerts(client: httpx.AsyncClient) -> Optional[dict]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = await client.get(ALERTS_API_URL, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            return data.get("states", {})
        except httpx.HTTPStatusError as e:
            logger.warning("HTTP %s від API (спроба %d/%d)", e.response.status_code, attempt, MAX_RETRIES)
        except httpx.RequestError as e:
            logger.warning("Помилка запиту: %s (спроба %d/%d)", e, attempt, MAX_RETRIES)
        except Exception as e:
            logger.error("Несподівана помилка: %s (спроба %d/%d)", e, attempt, MAX_RETRIES)

        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_DELAY)

    logger.error("API недоступний після %d спроб", MAX_RETRIES)
    return None

# ---------------------------------------------------------------------------
# Надсилання / редагування
# ---------------------------------------------------------------------------

async def send_wave(bot, wave: AlertWave) -> None:
    try:
        sent = await bot.send_message(
            chat_id=CHAT_ID,
            text=build_wave_message(wave),
            parse_mode=ParseMode.HTML,
        )
        wave.message_id = sent.message_id
        logger.info("📨 Надіслано хвилю: %s", sorted(wave.regions))
    except Exception as e:
        logger.error("Помилка надсилання: %s", e)


async def edit_wave(bot, wave: AlertWave) -> None:
    if wave.message_id is None:
        return
    try:
        await bot.edit_message_text(
            chat_id=CHAT_ID,
            message_id=wave.message_id,
            text=build_wave_message(wave),
            parse_mode=ParseMode.HTML,
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning("Не вдалося редагувати: %s", e)
    except Exception as e:
        logger.error("Помилка редагування: %s", e)

# ---------------------------------------------------------------------------
# Логіка тривог
# ---------------------------------------------------------------------------

async def check_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    global current_wave, active_regions, is_first_run

    client: httpx.AsyncClient = context.bot_data["http_client"]
    states = await fetch_alerts(client)
    if states is None:
        return

    new_active: set[str] = {
        name for name, params in states.items()
        if params.get("alertnow")
    }

    # Перший запуск — мовчки запам'ятовуємо стан
    if is_first_run:
        is_first_run = False
        active_regions = new_active
        logger.info("Початковий стан: %d активних областей", len(active_regions))
        return

    t = now_kyiv()
    now = datetime.now(timezone.utc)

    # Нові тривоги
    new_alerts = new_active - active_regions
    if new_alerts:
        # Чи є активна хвиля в межах вікна?
        wave_age = (now - current_wave.started_at).total_seconds() if current_wave else float("inf")

        if current_wave and wave_age <= WAVE_WINDOW_SEC:
            # Додаємо до поточної хвилі
            current_wave.regions |= new_alerts
            await edit_wave(context.bot, current_wave)
            logger.info("➕ Додано до хвилі: %s", sorted(new_alerts))
        else:
            # Нова хвиля
            current_wave = AlertWave(regions=set(new_alerts))
            await send_wave(context.bot, current_wave)
            logger.info("🌊 Нова хвиля: %s", sorted(new_alerts))

    # Відбій
    cleared = active_regions - new_active
    if cleared:
        for name in cleared:
            logger.info("✅ Відбій: %s", name)
            # Знаходимо хвилю де була ця область
            if current_wave and name in current_wave.regions and name not in current_wave.cleared:
                current_wave.cleared[name] = t
                await edit_wave(context.bot, current_wave)

    active_regions = new_active

# ---------------------------------------------------------------------------
# Команди
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Бот тривог запущено 🚨")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if active_regions:
        lines = ["<b>🚨 Активні тривоги:</b>\n"]
        for name in sorted(active_regions):
            lines.append(f"🚨 {name}")
        msg = "\n".join(lines)
    else:
        msg = "✅ <b>Наразі тривог немає</b>"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def post_init(application) -> None:
    application.bot_data["http_client"] = httpx.AsyncClient()
    application.job_queue.run_repeating(
        check_alerts,
        interval=POLL_INTERVAL,
        first=0,
        name="alerts_loop",
    )
    logger.info("Бот запущено. Інтервал: %ds", POLL_INTERVAL)


async def post_shutdown(application) -> None:
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


