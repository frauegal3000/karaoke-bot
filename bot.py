"""
Karaoke Queue Telegram Bot — button-driven, in-memory version (no database).

Everything is driven by keyboard buttons instead of typed commands. One
guided conversation exists:
  - "Add song": asks for a Spotify/YouTube link (name taken from Telegram handle).
  - "I'm an admin": asks for the password, then unlocks the admin menu.

State (queue, admin logins) lives in memory only — restarting the
process clears everything. Fine for a single karaoke night, not for
long-term persistence.

Environment variables required:
    TELEGRAM_BOT_TOKEN  — from BotFather
    ADMIN_PASSWORD      — password for admin login

Run:
    pip install -r requirements.txt
    python bot.py
"""

import logging
import os
import re
from typing import Optional

from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


TELEGRAM_BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
ADMIN_PASSWORD = _require_env("ADMIN_PASSWORD")

MAX_SONGS_WHEN_QUEUE_SMALL = 2
MAX_SONGS_WHEN_QUEUE_LARGE = 1
QUEUE_SIZE_THRESHOLD = 5

SPOTIFY_URL_RE = re.compile(
    r"^https?://(open\.)?spotify\.com/(intl-\w+/)?track/[\w]+", re.IGNORECASE
)
YOUTUBE_URL_RE = re.compile(
    r"^https?://(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w-]+", re.IGNORECASE
)

WELCOME_MESSAGE = (
    "🎤 Welcome to the Karaoke Queue Bot!\n\n"
    "Send a Spotify link to your song if it has lyrics attached — we're using "
    "AI to remove vocals from original songs, so audio quality is way better "
    "than typical karaoke tracks. YouTube links also work if that's easier.\n\n"
    "Use the buttons below to get started."
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

queue: list[dict] = []
admin_user_ids: set[int] = set()

# Conversation states
ASK_LINK = 0
ASK_PASSWORD = 1

# Button labels (also used for exact-text matching)
BTN_ADD_SONG = "🎤 Add song"
BTN_STATUS = "📊 My status"
BTN_SKIP = "⏭ Skip my turn"
BTN_PERFORMED = "✅ I performed"
BTN_SHOW_QUEUE = "👀 Show full queue"
BTN_ADMIN_LOGIN = "🔑 I'm an admin"
BTN_CANCEL = "❌ Cancel"

BTN_ADMIN_QUEUE = "📋 Full queue (admin)"
BTN_ADMIN_ADVANCE = "⏭ Advance queue (admin)"
BTN_ADMIN_REMOVE = "🗑 Remove participant (admin)"
BTN_ADMIN_PING = "📣 Ping next (admin)"
BTN_ADMIN_RESTART = "🔄 Restart session (admin)"


def _current_limit() -> int:
    if len(queue) >= QUEUE_SIZE_THRESHOLD:
        return MAX_SONGS_WHEN_QUEUE_LARGE
    return MAX_SONGS_WHEN_QUEUE_SMALL


def _classify_link(link: str) -> Optional[str]:
    if SPOTIFY_URL_RE.match(link):
        return "spotify"
    if YOUTUBE_URL_RE.match(link):
        return "youtube"
    return None


def _get_display_name(user) -> str:
    """Get display name from Telegram user: prefer username, fallback to first_name."""
    if user.username:
        return f"@{user.username}"
    return user.first_name or "Anonymous"


def _main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [BTN_ADD_SONG, BTN_STATUS],
        [BTN_SKIP, BTN_PERFORMED],
        [BTN_SHOW_QUEUE, BTN_ADMIN_LOGIN],
    ]
    if user_id in admin_user_ids:
        rows.append([BTN_ADMIN_QUEUE, BTN_ADMIN_ADVANCE])
        rows.append([BTN_ADMIN_REMOVE, BTN_ADMIN_PING])
        rows.append([BTN_ADMIN_RESTART])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True)


# ---------------------------------------------------------------------------
# Basic commands / menu
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        WELCOME_MESSAGE, reply_markup=_main_keyboard(update.effective_user.id)
    )


# ---------------------------------------------------------------------------
# "Add song" conversation (only asks for link, name from Telegram handle)
# ---------------------------------------------------------------------------

async def add_song_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    telegram_user_id = update.effective_user.id
    existing_count = sum(1 for s in queue if s["telegram_user_id"] == telegram_user_id)

    if existing_count >= _current_limit():
        await update.message.reply_text(
            "Queue is busy — you already have a song queued. Wait for your turn!",
            reply_markup=_main_keyboard(telegram_user_id),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Send a Spotify or YouTube link to the song.",
        reply_markup=_cancel_keyboard(),
    )
    return ASK_LINK


async def add_song_got_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    link = update.message.text.strip()
    source = _classify_link(link)

    if source is None:
        await update.message.reply_text(
            "That doesn't look like a valid Spotify or YouTube link — try again, "
            "or tap Cancel."
        )
        return ASK_LINK

    telegram_user_id = update.effective_user.id
    existing_count = sum(1 for s in queue if s["telegram_user_id"] == telegram_user_id)

    if existing_count >= _current_limit():
        await update.message.reply_text(
            "Queue is busy — you already have a song queued. Wait for your turn!",
            reply_markup=_main_keyboard(telegram_user_id),
        )
        return ConversationHandler.END

    display_name = _get_display_name(update.effective_user)
    queue.append(
        {
            "telegram_user_id": telegram_user_id,
            "display_name": display_name,
            "song_title": link,
            "artist": "",
            "link": link,
            "source": source,
        }
    )

    rank = len(queue)
    ahead = rank - 1
    people_word = "person" if ahead == 1 else "people"
    await update.message.reply_text(
        f"Added! You're #{rank} in the queue. {ahead} {people_word} ahead of you.",
        reply_markup=_main_keyboard(telegram_user_id),
    )
    return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Cancelled.", reply_markup=_main_keyboard(update.effective_user.id)
    )
    return ConversationHandler.END


add_song_conversation = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex(f"^{re.escape(BTN_ADD_SONG)}$"), add_song_entry)],
    states={
        ASK_LINK: [
            MessageHandler(filters.Regex(f"^{re.escape(BTN_CANCEL)}$"), cancel_conversation),
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_song_got_link),
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel_conversation)],
    allow_reentry=True,
)


# ---------------------------------------------------------------------------
# Simple one-tap user actions
# ---------------------------------------------------------------------------

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user_id = update.effective_user.id
    user_entries = [(i, s) for i, s in enumerate(queue) if s["telegram_user_id"] == telegram_user_id]

    if not user_entries:
        await update.message.reply_text("You don't have any songs in the queue yet.")
        return

    idx, entry = user_entries[0]
    if idx == 0:
        await update.message.reply_text("You're up next! 🎤")
        return

    person_before = queue[idx - 1]
    people_word = "person" if idx == 1 else "people"
    await update.message.reply_text(
        f"You're #{idx + 1} in the queue. {idx} {people_word} ahead of you. "
        f"The person before you: {person_before['display_name']}."
    )


async def skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user_id = update.effective_user.id
    user_entries = [(i, s) for i, s in enumerate(queue) if s["telegram_user_id"] == telegram_user_id]

    if not user_entries:
        await update.message.reply_text("You don't have any songs in the queue.")
        return

    if len(user_entries) == 1:
        idx, removed = user_entries[0]
        queue.pop(idx)
        await update.message.reply_text("Removed your song from the queue.")
        await _notify_new_front_of_queue(context)
    else:
        keyboard = [
            [InlineKeyboardButton(f"❌ {s['link'][:40]}...", callback_data=f"skip:{i}")]
            for i, s in user_entries
        ]
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
        await update.message.reply_text(
            "Which song do you want to remove?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def performed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user_id = update.effective_user.id
    user_entries = [(i, s) for i, s in enumerate(queue) if s["telegram_user_id"] == telegram_user_id]

    if not user_entries:
        await update.message.reply_text("You don't have any songs in the queue.")
        return

    if len(user_entries) == 1:
        idx, song = user_entries[0]
        if idx != 0:
            await update.message.reply_text("It isn't your turn yet.")
            return
        queue.pop(idx)
        await update.message.reply_text(f"Nice job, {song['display_name']}! 🎤")
        await _notify_new_front_of_queue(context)
    else:
        keyboard = [
            [InlineKeyboardButton(f"🎤 {s['link'][:40]}...", callback_data=f"performed:{i}")]
            for i, s in user_entries
        ]
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
        await update.message.reply_text(
            "Which song did you perform?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def show_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not queue:
        await update.message.reply_text("The queue is empty.")
        return

    lines = [
        f"{i}. {s['display_name']}, {s['link']}"
        for i, s in enumerate(queue, start=1)
    ]
    await update.message.reply_text("Current queue:\n" + "\n".join(lines))


async def _notify_new_front_of_queue(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not queue:
        return
    next_up = queue[0]
    try:
        await context.bot.send_message(
            chat_id=next_up["telegram_user_id"],
            text=f"It's your turn! Head to the stage. Link: {next_up['link']}",
        )
    except Exception:
        logger.exception(
            "Failed to notify user %s that it's their turn",
            next_up["telegram_user_id"],
        )


# ---------------------------------------------------------------------------
# Admin login conversation
# ---------------------------------------------------------------------------

async def admin_login_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id in admin_user_ids:
        await update.message.reply_text(
            "You're already authenticated as admin.",
            reply_markup=_main_keyboard(update.effective_user.id),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Send the admin password:", reply_markup=_cancel_keyboard()
    )
    return ASK_PASSWORD


async def admin_login_got_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text.strip()
    telegram_user_id = update.effective_user.id

    if password != ADMIN_PASSWORD:
        await update.message.reply_text(
            "Incorrect password.", reply_markup=_main_keyboard(telegram_user_id)
        )
        return ConversationHandler.END

    admin_user_ids.add(telegram_user_id)
    await update.message.reply_text(
        "You're now authenticated as admin. Admin buttons unlocked below.",
        reply_markup=_main_keyboard(telegram_user_id),
    )
    return ConversationHandler.END


admin_login_conversation = ConversationHandler(
    entry_points=[
        MessageHandler(filters.Regex(f"^{re.escape(BTN_ADMIN_LOGIN)}$"), admin_login_entry)
    ],
    states={
        ASK_PASSWORD: [
            MessageHandler(filters.Regex(f"^{re.escape(BTN_CANCEL)}$"), cancel_conversation),
            MessageHandler(filters.TEXT & ~filters.COMMAND, admin_login_got_password),
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel_conversation)],
    allow_reentry=True,
)


# ---------------------------------------------------------------------------
# Admin one-tap actions + inline keyboards
# ---------------------------------------------------------------------------

async def _require_admin(update: Update) -> bool:
    if update.effective_user.id not in admin_user_ids:
        await update.message.reply_text(
            "You need to authenticate first — tap 🔑 I'm an admin."
        )
        return False
    return True


async def admin_queue_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    if not queue:
        await update.message.reply_text("The queue is empty.")
        return

    lines = [
        f"{i}. {s['display_name']}, {s['link']}"
        for i, s in enumerate(queue, start=1)
    ]
    await update.message.reply_text("Full queue:\n" + "\n".join(lines))


async def admin_advance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    if not queue:
        await update.message.reply_text("The queue is empty — nothing to advance.")
        return

    current = queue[0]
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes, advance", callback_data="advance:confirm"),
            InlineKeyboardButton("Cancel", callback_data="cancel"),
        ]
    ]
    await update.message.reply_text(
        f"Are you sure you want to skip {current['display_name']} and advance the queue?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_remove_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    if not queue:
        await update.message.reply_text("The queue is empty.")
        return

    keyboard = [
        [InlineKeyboardButton(f"❌ {s['display_name']}", callback_data=f"remove:{i}")]
        for i, s in enumerate(queue)
    ]
    await update.message.reply_text(
        "Tap a participant to remove them:", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def admin_ping_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    if not queue:
        await update.message.reply_text("The queue is empty.")
        return

    next_up = queue[0]
    try:
        await context.bot.send_message(
            chat_id=next_up["telegram_user_id"],
            text=(
                f"Hey {next_up['display_name']}! You're up next! "
                f"Please head to the stage. Link: {next_up['link']}"
            ),
        )
        await update.message.reply_text(f"Pinged {next_up['display_name']}.")
    except Exception:
        logger.exception("Failed to ping user %s", next_up["telegram_user_id"])
        await update.message.reply_text(f"Failed to ping {next_up['display_name']}.")


async def admin_restart_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes, restart", callback_data="restart:confirm"),
            InlineKeyboardButton("Cancel", callback_data="cancel"),
        ]
    ]
    await update.message.reply_text(
        "This clears the entire queue. Are you sure?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    telegram_user_id = query.from_user.id

    if data == "cancel":
        await query.edit_message_text("Cancelled.")
        return

    if data.startswith("skip:"):
        idx = int(data.split(":", 1)[1])
        if idx >= len(queue):
            await query.edit_message_text("That song is no longer in the queue.")
            return
        song = queue[idx]
        if song["telegram_user_id"] != telegram_user_id:
            await query.edit_message_text("That's not your song!")
            return
        queue.pop(idx)
        await query.edit_message_text("Removed your song from the queue.")
        await _notify_new_front_of_queue(context)
        return

    if data.startswith("performed:"):
        idx = int(data.split(":", 1)[1])
        if idx >= len(queue):
            await query.edit_message_text("That song is no longer in the queue.")
            return
        song = queue[idx]
        if song["telegram_user_id"] != telegram_user_id:
            await query.edit_message_text("That's not your song!")
            return
        if idx != 0:
            await query.edit_message_text("It's not your turn yet for that song.")
            return
        queue.pop(idx)
        await query.edit_message_text(f"Nice job, {song['display_name']}! 🎤")
        await _notify_new_front_of_queue(context)
        return

    # Admin-only actions below
    if telegram_user_id not in admin_user_ids:
        await query.edit_message_text("You need to authenticate as admin first.")
        return

    if data.startswith("remove:"):
        idx = int(data.split(":", 1)[1])
        if 0 <= idx < len(queue):
            removed = queue.pop(idx)
            await query.edit_message_text(f"Removed '{removed['display_name']}' from the queue.")
            try:
                await context.bot.send_message(
                    chat_id=removed["telegram_user_id"],
                    text="Your song has been removed from the queue by an admin."
                )
            except Exception:
                logger.warning("Failed to notify user %s about removal", removed["telegram_user_id"])
            await _notify_new_front_of_queue(context)
        else:
            await query.edit_message_text("That participant is no longer in the queue.")

    elif data == "advance:confirm":
        if not queue:
            await query.edit_message_text("The queue is already empty.")
            return
        skipped = queue.pop(0)
        await query.edit_message_text(f"Advanced. '{skipped['display_name']}' skipped.")
        try:
            await context.bot.send_message(
                chat_id=skipped["telegram_user_id"],
                text="Your song has been skipped by an admin."
            )
        except Exception:
            logger.warning("Failed to notify user %s about being skipped", skipped["telegram_user_id"])
        await _notify_new_front_of_queue(context)

    elif data == "restart:confirm":
        queue.clear()
        await query.edit_message_text(
            "Session restarted. Queue cleared. Ready for next karaoke night!"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))

    application.add_handler(add_song_conversation)
    application.add_handler(admin_login_conversation)

    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_STATUS)}$"), status))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_SKIP)}$"), skip))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_PERFORMED)}$"), performed))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_SHOW_QUEUE)}$"), show_queue))

    application.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(BTN_ADMIN_QUEUE)}$"), admin_queue_view)
    )
    application.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(BTN_ADMIN_ADVANCE)}$"), admin_advance)
    )
    application.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(BTN_ADMIN_REMOVE)}$"), admin_remove_menu)
    )
    application.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(BTN_ADMIN_PING)}$"), admin_ping_next)
    )
    application.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(BTN_ADMIN_RESTART)}$"), admin_restart_menu)
    )

    application.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("Starting Karaoke Queue Bot (button-driven, in-memory, polling)...")
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
