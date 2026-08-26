"""Entry point: builds the Telegram application, registers handlers, runs polling."""

import logging

from telegram.ext import Application, CommandHandler

from bot import config
from bot.database import close_pool, init_pool
from bot.handlers import admin, user

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def _post_init(application: Application) -> None:
    await init_pool()
    logger.info("Database pool initialized.")


async def _post_shutdown(application: Application) -> None:
    await close_pool()
    logger.info("Database pool closed.")


def build_application() -> Application:
    application = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # User commands
    application.add_handler(CommandHandler("start", user.start))
    application.add_handler(CommandHandler("add", user.add))
    application.add_handler(CommandHandler("status", user.status))
    application.add_handler(CommandHandler("skip", user.skip))
    application.add_handler(CommandHandler("done", user.done))
    application.add_handler(CommandHandler("history", user.history))

    # Admin commands
    application.add_handler(CommandHandler("admin", admin.admin_login))
    application.add_handler(CommandHandler("remove", admin.remove))
    application.add_handler(CommandHandler("queue", admin.queue_view))
    application.add_handler(CommandHandler("end", admin.end))

    return application


def main() -> None:
    application = build_application()
    logger.info("Starting Karaoke Queue Bot (polling)...")
    application.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
