"""Handlers for commands available to all users, in private chat."""

import logging
import re

from telegram import Update
from telegram.ext import ContextTypes

from bot import queue_manager

logger = logging.getLogger(__name__)

YOUTUBE_URL_RE = re.compile(
    r"^https?://(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w-]+", re.IGNORECASE
)

ADD_USAGE = "Usage: /add Name - Song Title - Artist - YouTube Link"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🎤 Welcome to the Karaoke Queue Bot!\n\n"
        "Commands:\n"
        "/add <name> - <song> - <artist> - <youtube_link> — join the queue\n"
        "/status — check your place in line\n"
        "/skip — remove yourself from the queue\n"
        "/done — mark your performance complete (advances the queue)\n"
        "/history — see who's performed tonight\n\n"
        "Example:\n"
        "/add John - Bohemian Rhapsody - Queen - https://youtube.com/watch?v=fJ9rUzIMcZQ"
    )


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args)
    parts = [p.strip() for p in text.split(" - ")]

    if len(parts) != 4 or not all(parts):
        await update.message.reply_text(ADD_USAGE)
        return

    display_name, song_title, artist, youtube_link = parts

    if not YOUTUBE_URL_RE.match(youtube_link):
        await update.message.reply_text("Please provide a valid YouTube link.")
        return

    telegram_user_id = update.effective_user.id

    try:
        rank = await queue_manager.add_submission(
            telegram_user_id=telegram_user_id,
            display_name=display_name,
            song_title=song_title,
            artist=artist,
            youtube_link=youtube_link,
        )
    except queue_manager.SubmissionLimitReached:
        await update.message.reply_text(
            "Queue is busy — you already have a song queued. Wait for your turn!"
        )
        return

    ahead = rank - 1
    people_word = "person" if ahead == 1 else "people"
    await update.message.reply_text(
        f"Added! You're #{rank} in the queue. {ahead} {people_word} ahead of you."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user_id = update.effective_user.id
    info = await queue_manager.get_status_for_user(telegram_user_id)

    if info is None:
        await update.message.reply_text(
            "You don't have any songs in the queue. Use /add to join!"
        )
        return

    ahead = info["ahead_count"]
    person_before = info["person_before"]

    if ahead == 0:
        message = "You're up next! 🎤"
    else:
        people_word = "person" if ahead == 1 else "people"
        message = f"You're #{info['rank']} in the queue. {ahead} {people_word} ahead of you."
        if person_before is not None:
            message += (
                f" The person before you: {person_before.display_name} "
                f"singing '{person_before.song_title}'."
            )

    await update.message.reply_text(message)


async def skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user_id = update.effective_user.id
    removed = await queue_manager.remove_submission_by_user(telegram_user_id)

    if removed is None:
        await update.message.reply_text("You don't have any songs in the queue.")
        return

    await update.message.reply_text(f"Removed '{removed.song_title}' from the queue.")
    await _notify_new_front_of_queue(context)


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user_id = update.effective_user.id
    completed = await queue_manager.complete_current_turn(telegram_user_id)

    if completed is None:
        await update.message.reply_text(
            "You don't have any songs in the queue, or it isn't your turn yet."
        )
        return

    await update.message.reply_text(
        f"Nice job on '{completed.song_title}'! You've been added to tonight's history."
    )
    await _notify_new_front_of_queue(context)


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    performed = await queue_manager.get_history()

    if not performed:
        await update.message.reply_text("No songs performed yet tonight.")
        return

    lines = [
        f"{i}. {row['display_name']} — '{row['song_title']}' ({row['artist']})"
        for i, row in enumerate(performed, start=1)
    ]
    await update.message.reply_text("Tonight's performances:\n" + "\n".join(lines))


async def _notify_new_front_of_queue(context: ContextTypes.DEFAULT_TYPE) -> None:
    """After a /done or /skip advances the queue, DM whoever is now first."""
    next_up = await queue_manager.get_next_performer()
    if next_up is None:
        return

    try:
        await context.bot.send_message(
            chat_id=next_up.telegram_user_id,
            text=(
                f"It's your turn! Head to the stage. "
                f"Song: {next_up.song_title} by {next_up.artist}. "
                f"Link: {next_up.youtube_link}"
            ),
        )
    except Exception:
        logger.exception(
            "Failed to notify user %s that it's their turn", next_up.telegram_user_id
        )
