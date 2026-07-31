import asyncio

from telegram.ext import Application, CommandHandler, MessageHandler, filters

from app.bot.config import bot_settings
from app.bot.handlers import ready_handler, start_handler, text_handler


def build_application() -> Application:
    if not bot_settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required to start the Telegram bot.")
    application = Application.builder().token(bot_settings.telegram_bot_token).build()

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("ready", ready_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    return application


async def run_bot() -> None:
    application = build_application()

    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    try:
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


def main() -> None:
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
