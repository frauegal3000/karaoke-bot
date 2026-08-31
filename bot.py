"""
Karaoke Queue Telegram Bot — single-file, in-memory version (no database).

State (queue, admin logins) lives entirely in memory for the
process lifetime. Restarting the bot clears everything — that's expected
for a single karaoke night, but worth knowing if the process gets restarted
mid-session.

Environment variables required:
    TELEGRAM_BOT_TOKEN  — from BotFather
    ADMIN_PASSWORD      — password for /admin

Run:
    pip install -r requirements.txt
    python bot.py
"""

import functools
import logging
import os
import re

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

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

WELCOME_MESSAGE = (
    "🎤 Welcome to the Karaoke Queue Bot!\n\n"
    "Send a Spotify link to your song if it has lyrics attached — we're using "
    "AI to remove vocals from original songs, so audio quality is way better "
    "than typical karaoke tracks. YouTube links also work if that's easier.\n\n"
    "Commands:\n"
    "/add <song> - <artist> - <link> — join the queue\n"
    "/status — check your place in line\n"
    "/skip — remove yourself from the queue\n"
    "/performed — mark your performance complete\n\n"
    "Example:\n"
    "/add Bohemian Rhapsody - Queen - https://open.spotify.com/track/xxxx"
)

SPOTIFY_URL_RE = re.compile(
    r"^https?://(open\.)?spotify\.com/(intl-\w+/)?track/[\w]+", re.IGNORECASE
)
YOUTUBE_URL_RE = re.compile(
    r"^https?://(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w-]+", re.IGNORECASE
)

ADD_USAGE = "Usage: /add Song Title - Artist - Spotify or YouTube Link"
NOT_AUTHENTICATED_MSG = "You need to authenticate first. Use /admin <password>"

# Callback data prefixes
CB_VIEW_QUEUE = "view_queue"
CB_DONE_SELECT = "done_select:"
CB_CONFIRM_ADVANCE = "confirm_advance:"
CB_CANCEL = "cancel"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

# Each submission: {telegram_user_id, display_name, song_title, artist, link, source}
queue: list[dict] = []
admin_user_ids: set[int] = set()


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


def _get_display_name(user) -> str:
    """Get display name from Telegram user: prefer username, fallback to first_name."""
    if user.username:
        return f"@{user.username}"
    return user.first_name or "Anonymous"


# ---------------------------------------------------------------------------
# User commands
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [[InlineKeyboardButton("Show Full Queue", callback_data=CB_VIEW_QUEUE)]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(WELCOME_MESSAGE, reply_markup=reply_markup)


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args)
    parts = [p.strip() for p in text.split(" - ")]

    if len(parts) != 3 or not all(parts):
        await update.message.reply_text(ADD_USAGE)
        return

    song_title, artist, link = parts
    source = _classify_link(link)

    if source is None:
        await update.message.reply_text(
            "Please provide a valid Spotify or YouTube link."
        )
        return

    telegram_user_id = update.effective_user.id
    display_name = _get_display_name(update.effective_user)
    existing_count = sum(1 for s in queue if s["telegram_user_id"] == telegram_user_id)

    if existing_count >= _current_limit():
        await update.message.reply_text(
            "Queue is busy — you already have a song queued. Wait for your turn!"
        )
        return

    queue.append(
        {
            "telegram_user_id": telegram_user_id,
            "display_name": display_name,
            "song_title": song_title,
            "artist": artist,
            "link": link,
            "source": source,
        }
    )

    rank = len(queue)
    ahead = rank - 1
    people_word = "person" if ahead == 1 else "people"
    await update.message.reply_text(
        f"Added! You're #{rank} in the queue. {ahead} {people_word} ahead of you."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user_id = update.effective_user.id
    user_entries = [(i, s) for i, s in enumerate(queue) if s["telegram_user_id"] == telegram_user_id]

    if not user_entries:
        await update.message.reply_text(
            "You don't have any songs in the queue. Use /add to join!"
        )
        return

    idx, entry = user_entries[0]
    ahead = idx
    if ahead == 0:
        await update.message.reply_text("You're up next! 🎤")
        return

    person_before = queue[idx - 1]
    people_word = "person" if ahead == 1 else "people"
    await update.message.reply_text(
        f"You're #{idx + 1} in the queue. {ahead} {people_word} ahead of you. "
        f"The person before you: {person_before['display_name']} "
        f"singing '{person_before['song_title']}'."
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
        await update.message.reply_text(f"Removed '{removed['song_title']}' from the queue.")
        await _notify_new_front_of_queue(context)
    else:
        keyboard = [
            [InlineKeyboardButton(
                f"{s['song_title']} - {s['artist']}",
                callback_data=f"skip_select:{i}"
            )]
            for i, s in user_entries
        ]
        keyboard.append([InlineKeyboardButton("Cancel", callback_data=CB_CANCEL)])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Which song do you want to remove?",
            reply_markup=reply_markup
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
        await update.message.reply_text(f"Nice job on '{song['song_title']}'! 🎤")
        await _notify_new_front_of_queue(context)
    else:
        keyboard = [
            [InlineKeyboardButton(
                f"{s['song_title']} - {s['artist']}",
                callback_data=f"{CB_DONE_SELECT}{i}"
            )]
            for i, s in user_entries
        ]
        keyboard.append([InlineKeyboardButton("Cancel", callback_data=CB_CANCEL)])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Which song did you perform?",
            reply_markup=reply_markup
        )


async def _notify_new_front_of_queue(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not queue:
        return

    next_up = queue[0]
    try:
        await context.bot.send_message(
            chat_id=next_up["telegram_user_id"],
            text=(
                f"It's your turn! Head to the stage. "
                f"Song: {next_up['song_title']} by {next_up['artist']}. "
                f"Link: {next_up['link']}"
            ),
        )
    except Exception:
        logger.exception(
            "Failed to notify user %s that it's their turn",
            next_up["telegram_user_id"],
        )


# ---------------------------------------------------------------------------
# Callback handlers
# ---------------------------------------------------------------------------

async def user_queue_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display queue to users."""
    query = update.callback_query
    await query.answer()

    if not queue:
        text = "The queue is empty."
    else:
        lines = [
            f"{i}. {s['display_name']} - {s['song_title']} ({s['artist']})"
            for i, s in enumerate(queue, start=1)
        ]
        text = "Current queue:\n" + "\n".join(lines)

    keyboard = [[InlineKeyboardButton("Refresh", callback_data=CB_VIEW_QUEUE)]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(text, reply_markup=reply_markup)


async def handle_done_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle user selecting which song they performed."""
    query = update.callback_query
    await query.answer()

    idx = int(query.data.replace(CB_DONE_SELECT, ""))
    telegram_user_id = update.effective_user.id

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
    await query.edit_message_text(f"Nice job on '{song['song_title']}'! 🎤")
    await _notify_new_front_of_queue(context)


async def handle_skip_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle user selecting which song to skip/remove."""
    query = update.callback_query
    await query.answer()

    idx = int(query.data.replace("skip_select:", ""))
    telegram_user_id = update.effective_user.id

    if idx >= len(queue):
        await query.edit_message_text("That song is no longer in the queue.")
        return

    song = queue[idx]

    if song["telegram_user_id"] != telegram_user_id:
        await query.edit_message_text("That's not your song!")
        return

    queue.pop(idx)
    await query.edit_message_text(f"Removed '{song['song_title']}' from the queue.")
    await _notify_new_front_of_queue(context)


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle cancel button press."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Cancelled.")


async def handle_advance_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin confirmation to advance queue."""
    query = update.callback_query
    await query.answer()

    if update.effective_user.id not in admin_user_ids:
        await query.edit_message_text("You are not authorized to do this.")
        return

    if not queue:
        await query.edit_message_text("The queue is already empty.")
        return

    skipped = queue.pop(0)
    await query.edit_message_text(
        f"Skipped {skipped['display_name']} ('{skipped['song_title']}')."
    )

    # Notify the skipped user
    try:
        await context.bot.send_message(
            chat_id=skipped["telegram_user_id"],
            text=f"Your song '{skipped['song_title']}' has been skipped by an admin."
        )
    except Exception:
        logger.warning("Failed to notify user %s about being skipped", skipped["telegram_user_id"])

    await _notify_new_front_of_queue(context)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route callback queries to appropriate handlers."""
    query = update.callback_query
    data = query.data

    if data == CB_VIEW_QUEUE:
        await user_queue_view(update, context)
    elif data.startswith(CB_DONE_SELECT):
        await handle_done_selection(update, context)
    elif data.startswith("skip_select:"):
        await handle_skip_selection(update, context)
    elif data.startswith(CB_CONFIRM_ADVANCE):
        await handle_advance_confirmation(update, context)
    elif data == CB_CANCEL:
        await handle_cancel(update, context)
    else:
        await query.answer("Unknown action")


# ---------------------------------------------------------------------------
# Admin commands
# ---------------------------------------------------------------------------

def require_admin(handler):
    @functools.wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in admin_user_ids:
            await update.message.reply_text(NOT_AUTHENTICATED_MSG)
            return
        return await handler(update, context)

    return wrapped


async def admin_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /admin <password>")
        return

    password = " ".join(context.args)
    if password != ADMIN_PASSWORD:
        await update.message.reply_text("Incorrect password.")
        return

    admin_user_ids.add(update.effective_user.id)
    await update.message.reply_text("You're now authenticated as admin.")


@require_admin
async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /remove <position or name>")
        return

    identifier = " ".join(context.args).strip()
    target_idx = None

    if identifier.isdigit():
        pos = int(identifier)
        if 1 <= pos <= len(queue):
            target_idx = pos - 1
    else:
        needle = identifier.lower()
        target_idx = next(
            (i for i, s in enumerate(queue) if needle in s["display_name"].lower()),
            None,
        )

    if target_idx is None:
        await update.message.reply_text(f"No matching submission found for '{identifier}'.")
        return

    removed = queue.pop(target_idx)
    await update.message.reply_text(
        f"Removed '{removed['song_title']}' by {removed['display_name']} from the queue."
    )

    # Notify the removed user via DM
    try:
        await context.bot.send_message(
            chat_id=removed["telegram_user_id"],
            text=f"Your song '{removed['song_title']}' has been removed from the queue by an admin."
        )
    except Exception:
        logger.warning("Failed to notify user %s about removal", removed["telegram_user_id"])

    await _notify_new_front_of_queue(context)


@require_admin
async def queue_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not queue:
        await update.message.reply_text("The queue is empty.")
        return

    lines = [
        f"{i}. {s['display_name']}, {s['link']}"
        for i, s in enumerate(queue, start=1)
    ]
    await update.message.reply_text("Full queue:\n" + "\n".join(lines))


@require_admin
async def ping_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ping the next participant in the queue."""
    if not queue:
        await update.message.reply_text("The queue is empty.")
        return

    next_up = queue[0]
    try:
        await context.bot.send_message(
            chat_id=next_up["telegram_user_id"],
            text=(
                f"Hey {next_up['display_name']}! You're up next! "
                f"Please head to the stage. "
                f"Song: {next_up['song_title']} by {next_up['artist']}. "
                f"Link: {next_up['link']}"
            ),
        )
        await update.message.reply_text(f"Pinged {next_up['display_name']}.")
    except Exception:
        logger.exception("Failed to ping user %s", next_up["telegram_user_id"])
        await update.message.reply_text(f"Failed to ping {next_up['display_name']}.")


@require_admin
async def advance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show confirmation before advancing the queue (skipping current performer)."""
    if not queue:
        await update.message.reply_text("The queue is empty.")
        return

    current = queue[0]
    keyboard = [
        [
            InlineKeyboardButton("Yes, advance", callback_data=f"{CB_CONFIRM_ADVANCE}yes"),
            InlineKeyboardButton("Cancel", callback_data=CB_CANCEL),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Are you sure you want to skip {current['display_name']} "
        f"('{current['song_title']}') and advance the queue?",
        reply_markup=reply_markup
    )


@require_admin
async def end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    queue.clear()
    await update.message.reply_text(
        "Session ended. Queue cleared. Ready for next karaoke night!"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # User commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", add))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("skip", skip))
    application.add_handler(CommandHandler("performed", performed))

    # Admin commands
    application.add_handler(CommandHandler("admin", admin_login))
    application.add_handler(CommandHandler("remove", remove))
    application.add_handler(CommandHandler("queue", queue_view))
    application.add_handler(CommandHandler("end", end))
    application.add_handler(CommandHandler("ping", ping_next))
    application.add_handler(CommandHandler("advance", advance))

    # Callback query handler for inline keyboards
    application.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Starting Karaoke Queue Bot (in-memory, polling)...")
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
