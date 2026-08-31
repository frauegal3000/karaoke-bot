"""
Karaoke Queue Telegram Bot — single-file, in-memory version (no database).

State (queue, performed songs, admin logins) lives entirely in memory for the
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
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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

WELCOME_MESSAGE = (
    "🎤 Welcome to the Karaoke Queue Bot!\n\n"
    "Send a Spotify link to your song if it has lyrics attached — we're using "
    "AI to remove vocals from original songs, so audio quality is way better "
    "than typical karaoke tracks. YouTube links also work if that's easier.\n\n"
    "Commands:\n"
    "/add <name> - <song> - <artist> - <link> — join the queue\n"
    "/status — check your place in line\n"
    "/skip — remove yourself from the queue\n"
    "/done — mark your performance complete (advances the queue)\n"
    "/history — see who's performed tonight\n\n"
    "Example:\n"
    "/add John - Bohemian Rhapsody - Queen - https://open.spotify.com/track/xxxx"
)

SPOTIFY_URL_RE = re.compile(
    r"^https?://(open\.)?spotify\.com/(intl-\w+/)?track/[\w]+", re.IGNORECASE
)
YOUTUBE_URL_RE = re.compile(
    r"^https?://(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w-]+", re.IGNORECASE
)

ADD_USAGE = "Usage: /add Name - Song Title - Artist - Spotify or YouTube Link"
NOT_AUTHENTICATED_MSG = "You need to authenticate first. Use /admin <password>"

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
performed_songs: list[dict] = []
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


# ---------------------------------------------------------------------------
# User commands
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME_MESSAGE)


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args)
    parts = [p.strip() for p in text.split(" - ")]

    if len(parts) != 4 or not all(parts):
        await update.message.reply_text(ADD_USAGE)
        return

    display_name, song_title, artist, link = parts
    source = _classify_link(link)

    if source is None:
        await update.message.reply_text(
            "Please provide a valid Spotify or YouTube link."
        )
        return

    telegram_user_id = update.effective_user.id
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
    idx = next(
        (i for i, s in enumerate(queue) if s["telegram_user_id"] == telegram_user_id),
        None,
    )

    if idx is None:
        await update.message.reply_text(
            "You don't have any songs in the queue. Use /add to join!"
        )
        return

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
    idx = next(
        (i for i, s in enumerate(queue) if s["telegram_user_id"] == telegram_user_id),
        None,
    )

    if idx is None:
        await update.message.reply_text("You don't have any songs in the queue.")
        return

    removed = queue.pop(idx)
    await update.message.reply_text(f"Removed '{removed['song_title']}' from the queue.")
    await _notify_new_front_of_queue(context)


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user_id = update.effective_user.id

    if not queue or queue[0]["telegram_user_id"] != telegram_user_id:
        await update.message.reply_text(
            "You don't have any songs in the queue, or it isn't your turn yet."
        )
        return

    completed = queue.pop(0)
    performed_songs.append(
        {
            "display_name": completed["display_name"],
            "song_title": completed["song_title"],
            "artist": completed["artist"],
        }
    )
    await update.message.reply_text(
        f"Nice job on '{completed['song_title']}'! You've been added to tonight's history."
    )
    await _notify_new_front_of_queue(context)


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not performed_songs:
        await update.message.reply_text("No songs performed yet tonight.")
        return

    lines = [
        f"{i}. {row['display_name']} — '{row['song_title']}' ({row['artist']})"
        for i, row in enumerate(performed_songs, start=1)
    ]
    await update.message.reply_text("Tonight's performances:\n" + "\n".join(lines))


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


@require_admin
async def queue_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not queue:
        await update.message.reply_text("The queue is empty.")
        return

    lines = [
        f"{i}. {s['display_name']} — '{s['song_title']}' ({s['artist']}) "
        f"[{s['source']}] [user_id={s['telegram_user_id']}] {s['link']}"
        for i, s in enumerate(queue, start=1)
    ]
    await update.message.reply_text("Full queue:\n" + "\n".join(lines))


@require_admin
async def end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    queue.clear()
    performed_songs.clear()
    await update.message.reply_text(
        "Session ended. Queue and history cleared. Ready for next karaoke night!"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", add))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("skip", skip))
    application.add_handler(CommandHandler("done", done))
    application.add_handler(CommandHandler("history", history))

    application.add_handler(CommandHandler("admin", admin_login))
    application.add_handler(CommandHandler("remove", remove))
    application.add_handler(CommandHandler("queue", queue_view))
    application.add_handler(CommandHandler("end", end))

    logger.info("Starting Karaoke Queue Bot (in-memory, polling)...")
    application.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
