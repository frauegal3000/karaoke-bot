"""
Karaoke Queue Telegram Bot — button-driven, in-memory version (no database).

Everything is driven by keyboard buttons instead of typed commands. Two
guided conversations exist:
  - "Add song": asks for name, then a Spotify/YouTube link, step by step.
  - "I'm an admin": asks for the password, then unlocks the admin menu.

State (queue, history, admin logins) lives in memory only — restarting the
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
QUEUE_SIZE_THRESHOLD = 10

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

# Each submission: {telegram_user_id, display_name, song_title, artist, link, source}
# Note: since users add songs via buttons rather than a structured command,
# song_title/artist aren't separately collected — the link speaks for itself,
# and "song_title" doubles as a short label built from the display name.
queue: list[dict] = []
performed_songs: list[dict] = []
admin_user_ids: set[int] = set()

# Conversation states
ASK_NAME, ASK_LINK = range(2)
ASK_PASSWORD = range(2, 3)[0]

# Button labels (also used for exact-text matching)
BTN_ADD_SONG = "🎤 Add song"
BTN_STATUS = "📊 My status"
BTN_SKIP = "⏭ Skip my turn"
BTN_DONE = "✅ Mark as done"
BTN_HISTORY = "📜 History"
BTN_ADMIN_LOGIN = "🔑 I'm an admin"
BTN_CANCEL = "❌ Cancel"

BTN_ADMIN_QUEUE = "📋 Full queue (admin)"
BTN_ADMIN_ADVANCE = "⏭ Advance queue (admin)"
BTN_ADMIN_REMOVE = "🗑 Remove participant (admin)"
BTN_ADMIN_RESTART = "🔄 Restart session (admin)"


def _current_limit() -> int:
    if len(queue) >= QUEUE_SIZE_THRESHOLD:
        return MAX_SONGS_WHEN_QUEUE_LARGE
    return MAX_SONGS_WHEN_QUEUE_SMALL


def _classify_link(link: str) -> str | None:
    if SPOTIFY_URL_RE.match(link):
        return "spotify"
    if YOUTUBE_URL_RE.match(link):
        return "youtube"
    return None


def _main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [BTN_ADD_SONG, BTN_STATUS],
        [BTN_SKIP, BTN_DONE],
        [BTN_HISTORY, BTN_ADMIN_LOGIN],
    ]
    if user_id in admin_user_ids:
        rows.append([BTN_ADMIN_QUEUE, BTN_ADMIN_ADVANCE])
        rows.append([BTN_ADMIN_REMOVE, BTN_ADMIN_RESTART])
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
# "Add song" conversation
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
        "What name should show up in the queue?", reply_markup=_cancel_keyboard()
    )
    return ASK_NAME


async def add_song_got_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Please send a name (or tap Cancel).")
        return ASK_NAME

    context.user_data["pending_name"] = name
    await update.message.reply_text(
        "Now send a Spotify or YouTube link to the song.",
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
        context.user_data.pop("pending_name", None)
        return ConversationHandler.END

    display_name = context.user_data.pop("pending_name", "Someone")
    queue.append(
        {
            "telegram_user_id": telegram_user_id,
            "display_name": display_name,
            "song_title": link,  # link doubles as the identifying detail
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
    context.user_data.pop("pending_name", None)
    await update.message.reply_text(
        "Cancelled.", reply_markup=_main_keyboard(update.effective_user.id)
    )
    return ConversationHandler.END


add_song_conversation = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex(f"^{re.escape(BTN_ADD_SONG)}$"), add_song_entry)],
    states={
        ASK_NAME: [
            MessageHandler(filters.Regex(f"^{re.escape(BTN_CANCEL)}$"), cancel_conversation),
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_song_got_name),
        ],
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
    idx = next(
        (i for i, s in enumerate(queue) if s["telegram_user_id"] == telegram_user_id),
        None,
    )

    if idx is None:
        await update.message.reply_text("You don't have any songs in the queue yet.")
        return

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
    idx = next(
        (i for i, s in enumerate(queue) if s["telegram_user_id"] == telegram_user_id),
        None,
    )

    if idx is None:
        await update.message.reply_text("You don't have any songs in the queue.")
        return

    removed = queue.pop(idx)
    await update.message.reply_text(f"Removed '{removed['display_name']}' from the queue.")
    await _notify_new_front_of_queue(context)


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user_id = update.effective_user.id

    if not queue or queue[0]["telegram_user_id"] != telegram_user_id:
        await update.message.reply_text(
            "You don't have a song in the queue, or it isn't your turn yet."
        )
        return

    completed = queue.pop(0)
    performed_songs.append(
        {"display_name": completed["display_name"], "link": completed["link"]}
    )
    await update.message.reply_text(
        f"Nice job, {completed['display_name']}! Added to tonight's history."
    )
    await _notify_new_front_of_queue(context)


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not performed_songs:
        await update.message.reply_text("No songs performed yet tonight.")
        return

    lines = [
        f"{i}. {row['display_name']}" for i, row in enumerate(performed_songs, start=1)
    ]
    await update.message.reply_text("Tonight's performances:\n" + "\n".join(lines))


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
        f"{i}. {s['display_name']} [{s['source']}] [user_id={s['telegram_user_id']}] {s['link']}"
        for i, s in enumerate(queue, start=1)
    ]
    await update.message.reply_text("Full queue:\n" + "\n".join(lines))


async def admin_advance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    if not queue:
        await update.message.reply_text("The queue is empty — nothing to advance.")
        return

    completed = queue.pop(0)
    performed_songs.append(
        {"display_name": completed["display_name"], "link": completed["link"]}
    )
    await update.message.reply_text(f"Advanced. '{completed['display_name']}' marked complete.")
    await _notify_new_front_of_queue(context)


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


async def admin_restart_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes, restart", callback_data="restart:confirm"),
            InlineKeyboardButton("Cancel", callback_data="restart:cancel"),
        ]
    ]
    await update.message.reply_text(
        "This clears the entire queue and history. Are you sure?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.from_user.id not in admin_user_ids:
        await query.edit_message_text("You need to authenticate as admin first.")
        return

    data = query.data

    if data.startswith("remove:"):
        idx = int(data.split(":", 1)[1])
        if 0 <= idx < len(queue):
            removed = queue.pop(idx)
            await query.edit_message_text(f"Removed '{removed['display_name']}' from the queue.")
        else:
            await query.edit_message_text("That participant is no longer in the queue.")

    elif data == "restart:confirm":
        queue.clear()
        performed_songs.clear()
        await query.edit_message_text(
            "Session restarted. Queue and history cleared. Ready for next karaoke night!"
        )

    elif data == "restart:cancel":
        await query.edit_message_text("Restart cancelled.")


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
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_DONE)}$"), done))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_HISTORY)}$"), history))

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
        MessageHandler(filters.Regex(f"^{re.escape(BTN_ADMIN_RESTART)}$"), admin_restart_menu)
    )

    application.add_handler(CallbackQueryHandler(admin_callback_handler))

    logger.info("Starting Karaoke Queue Bot (button-driven, in-memory, polling)...")
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
