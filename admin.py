"""Handlers for admin-only commands.

Admin auth is password-based and persisted in the `admin_sessions` table:
anyone who supplies the correct password is remembered as an admin (by their
Telegram user id) until the session is ended. This matches the spec's
"password-based, anyone with password can use admin commands" model.
"""

import functools
import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot import config, queue_manager
from bot.database import get_pool

logger = logging.getLogger(__name__)

NOT_AUTHENTICATED_MSG = "You need to authenticate first. Use /admin <password>"


async def _is_admin(telegram_user_id: int) -> bool:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT 1 FROM admin_sessions WHERE telegram_user_id = $1", telegram_user_id
    )
    return row is not None


def require_admin(handler):
    @functools.wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        telegram_user_id = update.effective_user.id
        if not await _is_admin(telegram_user_id):
            await update.message.reply_text(NOT_AUTHENTICATED_MSG)
            return
        return await handler(update, context)

    return wrapped


async def admin_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /admin <password>")
        return

    password = " ".join(context.args)
    if password != config.ADMIN_PASSWORD:
        await update.message.reply_text("Incorrect password.")
        return

    telegram_user_id = update.effective_user.id
    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO admin_sessions (telegram_user_id)
        VALUES ($1)
        ON CONFLICT (telegram_user_id) DO UPDATE SET authenticated_at = NOW()
        """,
        telegram_user_id,
    )
    await update.message.reply_text("You're now authenticated as admin.")


@require_admin
async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /remove <position or name>")
        return

    identifier = " ".join(context.args)
    removed = await queue_manager.remove_by_position_or_name(identifier)

    if removed is None:
        await update.message.reply_text(f"No matching submission found for '{identifier}'.")
        return

    await update.message.reply_text(
        f"Removed '{removed.song_title}' by {removed.display_name} from the queue."
    )


@require_admin
async def queue_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    queue = await queue_manager.get_queue()

    if not queue:
        await update.message.reply_text("The queue is empty.")
        return

    lines = [
        f"{i}. {s.display_name} — '{s.song_title}' ({s.artist}) "
        f"[user_id={s.telegram_user_id}] {s.youtube_link}"
        for i, s in enumerate(queue, start=1)
    ]
    await update.message.reply_text("Full queue:\n" + "\n".join(lines))


@require_admin
async def end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await queue_manager.end_session()
    await update.message.reply_text(
        "Session ended. Queue and history cleared. Ready for next karaoke night!"
    )
