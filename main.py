import os
import requests
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from telegram import Update

# ===========================
# Налаштування
# ===========================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
bot = Bot(token=BOT_TOKEN)

# ===========================
# Кеш останнього стану
# ===========================
last_status = {}  # region_name -> {status, threat, type}

# Іконки для типів тривоги
ICONS = {
    "AIR": "🛩️",
    "ARTILLERY": "💣",
    "DRONE": "🛰️",
    "URBAN_FIGHT": "⚔️",
    "UNKNOWN": "🚨"
}

# ===========================
# Очистка кешу при старті
# ===========================
def clear_cache():
    global last_status
    last_status = {}
    print("Кеш очищено!")

# ===========================
# Команда /start
# ===========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот увімкнено! Тривоги надходитимуть лише при зміні стану."
    )

# ===========================
# Основна функція перевірки тривог
# ===========================
async def check_alerts():
    global last_status
    try:
        response = requests.get("https://alerts.com.ua/api/states")
        regions = response.json()
    except Exception as e:
        print("Помилка при отриманні API:", e)
        return

    active_alerts = []
    changed = False

    for region in regions:
        region_name = region.get("name")
        status = region.get("alert", False)
        alert_type = region.get("type", "UNKNOWN")
        threat_detail = region.get("threat", "")
        icon = ICONS.get(alert_type, ICONS["UNKNOWN"])

        current_status = {"status": status, "threat": threat_detail, "type": alert_type}

        if last_status.get(region_name) != current_status:
            last_status[region_name] = current_status
            changed = True

        if status:
            threat_text = f" — {threat_detail}" if threat_detail else ""
            # Форматування жирним та емодзі
            active_alerts.append(f"{icon} <b>{region_name}</b> ({alert_type}){threat_text}")

    if changed:
        if active_alerts:
            message = "<b>🚨 Активні повітряні тривоги:</b>\n" + "\n".join(active_alerts)
        else:
            message = "✅ <b>Всі тривоги відбій</b>"

        try:
            await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode=ParseMode.HTML)
        except Exception as e:
            print("Помилка відправки:", e)

# ===========================
# Webhook обробник для JustRunMy.App
# ===========================
async def alerts_webhook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Викликається через cron або POST на /alerts
    await check_alerts()

# ===========================
# Запуск бота
# ===========================
async def main():
    clear_cache()
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Команда /start
    application.add_handler(CommandHandler("start", start))

    # Webhook endpoint
    application.add_handler(MessageHandler(filters.ALL, alerts_webhook))

    # Запуск бота
    await application.start()
    await application.updater.start_polling()
    await application.idle()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
