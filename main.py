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

# https://wiki.ubilling.net.ua/doku.php?id=aerialalertsapi
ALERTS_API_URL  = "https://ubilling.net.ua/aerialalerts/"
POLL_INTERVAL   = 5    # секунди (API кешує 3с, ліміт 2 rps)
REQUEST_TIMEOUT = 10
MAX_RETRIES     = 3
RETRY_DELAY     = 5

# ---------------------------------------------------------------------------
# Типи
# ---------------------------------------------------------------------------

ICONS: dict[str, str] = {
    "AIR":         "🛩️",
    "ARTILLERY":   "💣",
    "DRONE":       "🛰️",
    "URBAN_FIGHT": "⚔️",
    "UNKNOWN":     "🚨",
}

@dataclass
class RegionAlert:
    active: bool
    alert_type: str
    message_id: Optional[int] = None
    updates: list[str] = field(default_factory=list)

# ---------------------------------------------------------------------------
# Стан
# ---------------------------------------------------------------------------

region_alerts: dict[str, RegionAlert] = {}

# ---------------------------------------------------------------------------
# Утіліти
# ---------------------------------------------------------------------------

def now_kyiv() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%H:%M")


def format_message(name: str, alert: RegionAlert) -> str:
    icon = ICONS.get(alert.alert_type, "🚨")
    lines = [f"‼️{icon} <b>Тривога у {name}</b>"]
    lines.extend(alert.updates)
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

async def fetch_alerts(client: httpx.AsyncClient) -> Optional[dict]:
    """
    Повертає словник виду:
      { "Київська область": {"alertnow": True, "type": "DRONE", ...}, ... }
    або None при помилці.
    """
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
# Логіка тривог
# ---------------------------------------------------------------------------

async def check_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    global region_alerts

    client: httpx.AsyncClient = context.bot_data["http_client"]
    states = await fetch_alerts(client)
    if states is None:
        return

    for name, params in states.items():
        active: bool    = bool(params.get("alertnow", False))
        alert_type: str = params.get("type", "UNKNOWN") or "UNKNOWN"

        existing = region_alerts.get(name)

        # --- Нова тривога ---
        if active and (existing is None or not existing.active):
            alert = RegionAlert(active=True, alert_type=alert_type)
            region_alerts[name] = alert

            try:
                sent = await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text=format_message(name, alert),
                    parse_mode=ParseMode.HTML,
                )
                alert.message_id = sent.message_id
                logger.info("🚨 Тривога: %s (%s)", name, alert_type)
            except Exception as e:
                logger.error("Помилка надсилання для %s: %s", name, e)

        # --- Відбій ---
        elif not active and existing and existing.active:
            t = now_kyiv()
            existing.active = False
            existing.updates.append(f"\n✅ <b>UPD {t}:</b> відбій тривоги")

            if existing.message_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=CHAT_ID,
                        message_id=existing.message_id,
                        text=format_message(name, existing),
                        parse_mode=ParseMode.HTML,
                    )
                    logger.info("✅ Відбій: %s", name)
                except BadRequest as e:
                    logger.warning("Не вдалося редагувати повідомлення для %s: %s", name, e)
                except Exception as e:
                    logger.error("Помилка редагування для %s: %s", name, e)

        # --- Зміна типу тривоги ---
        elif active and existing and existing.active and existing.alert_type != alert_type:
            t = now_kyiv()
            icon = ICONS.get(alert_type, "🚨")
            existing.alert_type = alert_type
            existing.updates.append(f"\n🔄 <b>UPD {t}:</b> тип змінився — {icon} {alert_type}")

            if existing.message_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=CHAT_ID,
                        message_id=existing.message_id,
                        text=format_message(name, existing),
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as e:
                    logger.error("Помилка редагування типу для %s: %s", name, e)

# ---------------------------------------------------------------------------
# Команди
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Бот тривог запущено 🚨")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    active = [
        f"{ICONS.get(a.alert_type, '🚨')} <b>{name}</b>"
        for name, a in region_alerts.items()
        if a.active
    ]
    if active:
        msg = "<b>🚨 Активні тривоги:</b>\n\n" + "\n".join(active)
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

