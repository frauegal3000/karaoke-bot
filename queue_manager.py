"""Queue business logic: adding, removing, advancing, and querying the queue.

Positions are assigned from a monotonically increasing sequence on insert, so
ordering by `position ASC` always reflects insertion order regardless of any
rows removed in between. Display rank (1-indexed "you're #N") is always
computed on the fly from that ordering rather than trusting the stored
`position` value directly, so gaps left by removals never show up to users.
"""

from dataclasses import dataclass
from datetime import datetime

from bot import config
from bot.database import get_pool


@dataclass
class Submission:
    id: int
    telegram_user_id: int
    display_name: str
    song_title: str
    artist: str
    youtube_link: str
    position: int
    created_at: datetime


class SubmissionLimitReached(Exception):
    """Raised when a user tries to add more songs than their current limit allows."""


async def _current_limit_for_new_submission(conn) -> int:
    queue_size = await conn.fetchval("SELECT COUNT(*) FROM queue")
    if queue_size >= config.QUEUE_SIZE_THRESHOLD:
        return config.MAX_SONGS_WHEN_QUEUE_LARGE
    return config.MAX_SONGS_WHEN_QUEUE_SMALL


async def add_submission(
    telegram_user_id: int,
    display_name: str,
    song_title: str,
    artist: str,
    youtube_link: str,
) -> int:
    """Add a submission to the end of the queue. Returns the user's 1-indexed rank.

    Raises SubmissionLimitReached if the user is already at their limit.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            limit = await _current_limit_for_new_submission(conn)
            existing_count = await conn.fetchval(
                "SELECT COUNT(*) FROM queue WHERE telegram_user_id = $1",
                telegram_user_id,
            )
            if existing_count >= limit:
                raise SubmissionLimitReached()

            next_position = await conn.fetchval("SELECT nextval('queue_position_seq')")
            await conn.execute(
                """
                INSERT INTO queue
                    (telegram_user_id, display_name, song_title, artist, youtube_link, position)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                telegram_user_id,
                display_name,
                song_title,
                artist,
                youtube_link,
                next_position,
            )

        rank = await conn.fetchval(
            """
            SELECT COUNT(*) FROM queue
            WHERE position <= (SELECT MAX(position) FROM queue WHERE telegram_user_id = $1)
            """,
            telegram_user_id,
        )
        return rank


async def get_queue() -> list[Submission]:
    pool = get_pool()
    rows = await pool.fetch("SELECT * FROM queue ORDER BY position ASC")
    return [Submission(**dict(row)) for row in rows]


async def get_status_for_user(telegram_user_id: int) -> dict | None:
    """Returns rank / who's ahead / who's right before them, or None if not queued."""
    queue = await get_queue()
    user_indices = [i for i, s in enumerate(queue) if s.telegram_user_id == telegram_user_id]
    if not user_indices:
        return None

    idx = user_indices[0]  # earliest submission belonging to this user
    ahead_count = idx
    person_before = queue[idx - 1] if idx > 0 else None

    return {
        "rank": idx + 1,
        "ahead_count": ahead_count,
        "person_before": person_before,
        "submission": queue[idx],
    }


async def remove_submission_by_user(telegram_user_id: int) -> Submission | None:
    """Used by /skip. Removes the user's earliest queued submission entirely
    (not added to performed history). Returns the removed row, or None if the
    user had nothing queued."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            DELETE FROM queue
            WHERE id = (
                SELECT id FROM queue
                WHERE telegram_user_id = $1
                ORDER BY position ASC
                LIMIT 1
            )
            RETURNING *
            """,
            telegram_user_id,
        )
    return Submission(**dict(row)) if row else None


async def complete_current_turn(telegram_user_id: int) -> Submission | None:
    """Used by /done. Only succeeds if the caller is first in queue. Moves
    them to performed_songs and removes them from the queue. Returns the
    completed submission, or None if the caller isn't first (or isn't queued)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            front = await conn.fetchrow(
                "SELECT * FROM queue ORDER BY position ASC LIMIT 1"
            )
            if front is None or front["telegram_user_id"] != telegram_user_id:
                return None

            await conn.execute(
                """
                INSERT INTO performed_songs (display_name, song_title, artist)
                VALUES ($1, $2, $3)
                """,
                front["display_name"],
                front["song_title"],
                front["artist"],
            )
            await conn.execute("DELETE FROM queue WHERE id = $1", front["id"])

    return Submission(**dict(front))


async def get_next_performer() -> Submission | None:
    """Returns whoever is now first in queue (used to notify after an advance)."""
    queue = await get_queue()
    return queue[0] if queue else None


async def remove_by_position_or_name(identifier: str) -> Submission | None:
    """Admin removal by 1-indexed queue position or by display name (case-insensitive
    substring match, first hit). Returns the removed row, or None if not found."""
    pool = get_pool()
    queue = await get_queue()
    if not queue:
        return None

    target_id = None
    if identifier.isdigit():
        pos = int(identifier)
        if 1 <= pos <= len(queue):
            target_id = queue[pos - 1].id
    else:
        needle = identifier.strip().lower()
        for s in queue:
            if needle in s.display_name.lower():
                target_id = s.id
                break

    if target_id is None:
        return None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM queue WHERE id = $1 RETURNING *", target_id
        )
    return Submission(**dict(row)) if row else None


async def get_history() -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT display_name, song_title, artist, performed_at "
        "FROM performed_songs ORDER BY performed_at ASC"
    )
    return [dict(row) for row in rows]


async def end_session() -> None:
    """Clears the queue and performed songs list."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE queue")
        await conn.execute("TRUNCATE performed_songs")
