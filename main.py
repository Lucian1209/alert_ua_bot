
import os
import requests
import asyncio

from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

bot = Bot(BOT_TOKEN)

# кеш стану
last_status = {}

ICONS = {
    "AIR": "🛩️",
    "ARTILLERY": "💣",
    "DRONE": "🛰️",
    "URBAN_FIGHT": "⚔️",
    "UNKNOWN": "🚨"
}

# -----------------------------

async def start(update, context):
    await update.message.reply_text("Бот тривог запущено 🚨")

# -----------------------------

async def check_alerts():

    global last_status

    try:
        r = requests.get("https://alerts.com.ua/api/states")
        regions = r.json()
    except:
        print("API error")
        return

    active = []
    changed = False

    for region in regions:

        name = region.get("name")
        status = region.get("alert", False)
        alert_type = region.get("type", "UNKNOWN")

        current = {
            "status": status,
            "type": alert_type
        }

        if last_status.get(name) != current:
            changed = True
            last_status[name] = current

        if status:
            icon = ICONS.get(alert_type, "🚨")
            active.append(f"{icon} <b>{name}</b>")

    if changed:

        if active:
            msg = "<b>🚨 Повітряна тривога:</b>\n\n" + "\n".join(active)
        else:
            msg = "✅ <b>Відбій тривоги по всій Україні</b>"

        await bot.send_message(
            chat_id=CHAT_ID,
            text=msg,
            parse_mode=ParseMode.HTML
        )

# -----------------------------

async def alerts_loop():

    while True:

        await check_alerts()

        await asyncio.sleep(30)

# -----------------------------

async def main():

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))

    asyncio.create_task(alerts_loop())

    await application.run_polling()

# -----------------------------

if __name__ == "__main__":
    asyncio.run(main())
