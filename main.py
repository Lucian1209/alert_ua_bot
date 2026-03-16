import os
import logging
import asyncio
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

ALERTS_API_URL  = "https://ubilling.net.ua/aerialalerts/?source=aiu"
POLL_INTERVAL   = 5
REQUEST_TIMEOUT = 10
MAX_RETRIES     = 3
RETRY_DELAY     = 5

# ---------------------------------------------------------------------------
# Іконки
# ---------------------------------------------------------------------------

ICONS: dict[str, str] = {
    "AIR":         "🛩️",
    "ARTILLERY":   "💣",
    "DRONE":       "🛰️",
    "URBAN_FIGHT": "⚔️",
    "UNKNOWN":     "🚨",
}

ALERT_LABELS: dict[str, str] = {
    "AIR":         "авіація",
    "ARTILLERY":   "артилерія",
    "DRONE":       "БПЛА",
    "URBAN_FIGHT": "вуличні бої",
    "UNKNOWN":     "невідома загроза",
}

# ---------------------------------------------------------------------------
# Стан
# ---------------------------------------------------------------------------

# Поточні активні регіони: { name: alert_type }
active_regions: dict[str, str] = {}

# ID єдиного повідомлення з тривогами
alert_message_id: Optional[int] = None

# Лог змін для UPD рядків
upd_log: list[str] = []

# Перший запуск — не пишемо UPD щоб не спамити
is_first_run: bool = True

# ---------------------------------------------------------------------------
# Утіліти
# ---------------------------------------------------------------------------

def now_kyiv() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%H:%M")


def build_message() -> str:
    lines = []

    if active_regions:
        lines.append("<b>🚨 Активні тривоги:</b>\n")
        for name, alert_type in sorted(active_regions.items()):
            icon = ICONS.get(alert_type, "🚨")
            lines.append(f"{icon} {name}")
    else:
        lines.append("✅ <b>Тривог немає</b>")

    if upd_log:
        lines.append("")
        lines.extend(upd_log)

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
# Надсилання / редагування повідомлення
# ---------------------------------------------------------------------------

async def send_or_edit(bot) -> None:
    global alert_message_id

    text = build_message()

    if alert_message_id is None:
        try:
            sent = await bot.send_message(
                chat_id=CHAT_ID,
                text=text,
                parse_mode=ParseMode.HTML,
            )
            alert_message_id = sent.message_id
        except Exception as e:
            logger.error("Помилка надсилання: %s", e)
    else:
        try:
            await bot.edit_message_text(
                chat_id=CHAT_ID,
                message_id=alert_message_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except BadRequest as e:
            if "Message is not modified" in str(e):
                pass  # нічого не змінилось — ок
            else:
                logger.warning("Не вдалося редагувати: %s", e)
        except Exception as e:
            logger.error("Помилка редагування: %s", e)

# ---------------------------------------------------------------------------
# Логіка тривог
# ---------------------------------------------------------------------------

async def check_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    global active_regions, upd_log, is_first_run

    client: httpx.AsyncClient = context.bot_data["http_client"]
    states = await fetch_alerts(client)
    if states is None:
        return

    new_active: dict[str, str] = {
        name: (params.get("type") or "UNKNOWN")
        for name, params in states.items()
        if params.get("alertnow")
    }

    # Перший запуск — просто зберігаємо стан і надсилаємо повідомлення без UPD
    if is_first_run:
        is_first_run = False
        active_regions = new_active
        if active_regions:
            await send_or_edit(context.bot)
        return

    t = now_kyiv()

    # Нові тривоги — групуємо в один UPD якщо кілька одночасно
    new_alerts = [name for name in new_active if name not in active_regions]
    if new_alerts:
        if len(new_alerts) == 1:
            name = new_alerts[0]
            alert_type = new_active[name]
            icon = ICONS.get(alert_type, "🚨")
            label = ALERT_LABELS.get(alert_type, "")
            type_str = f" ({label})" if label and alert_type != "UNKNOWN" else ""
            upd_log.append(f"⚠️ <b>UPD {t}:</b> тривога — {icon} {name}{type_str}")
        else:
            names_str = ", ".join(sorted(new_alerts))
            upd_log.append(f"⚠️ <b>UPD {t}:</b> тривога — {len(new_alerts)} областей: {names_str}")
        for name in new_alerts:
            logger.info("🚨 Тривога: %s (%s)", name, new_active[name])

    # Зміна типу
    for name, alert_type in new_active.items():
        if name in active_regions and active_regions[name] != alert_type and alert_type != "UNKNOWN":
            icon = ICONS.get(alert_type, "🚨")
            label = ALERT_LABELS.get(alert_type, alert_type)
            upd_log.append(f"🔄 <b>UPD {t}:</b> {icon} {name} — {label}")

    # Відбій — теж групуємо
    cleared = [name for name in active_regions if name not in new_active]
    if cleared:
        if len(cleared) == 1:
            upd_log.append(f"✅ <b>UPD {t}:</b> відбій — {cleared[0]}")
        else:
            names_str = ", ".join(sorted(cleared))
            upd_log.append(f"✅ <b>UPD {t}:</b> відбій — {len(cleared)} областей: {names_str}")
        for name in cleared:
            logger.info("✅ Відбій: %s", name)

    changed = bool(new_alerts or cleared or any(
        name in active_regions and active_regions[name] != t2 and t2 != "UNKNOWN"
        for name, t2 in new_active.items()
    ))

    if changed:
        active_regions = new_active
        await send_or_edit(context.bot)

# ---------------------------------------------------------------------------
# Команди
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Бот тривог запущено 🚨")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if active_regions:
        lines = ["<b>🚨 Активні тривоги:</b>\n"]
        for name, alert_type in sorted(active_regions.items()):
            icon = ICONS.get(alert_type, "🚨")
            lines.append(f"{icon} {name}")
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


