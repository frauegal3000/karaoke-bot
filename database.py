"""Database connection pooling and schema management (PostgreSQL via asyncpg)."""

import asyncpg

from bot import config

_pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id SERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    display_name TEXT NOT NULL,
    song_title TEXT NOT NULL,
    artist TEXT NOT NULL,
    youtube_link TEXT NOT NULL,
    position INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS performed_songs (
    id SERIAL PRIMARY KEY,
    display_name TEXT NOT NULL,
    song_title TEXT NOT NULL,
    artist TEXT NOT NULL,
    performed_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    telegram_user_id BIGINT PRIMARY KEY,
    authenticated_at TIMESTAMP DEFAULT NOW()
);

CREATE SEQUENCE IF NOT EXISTS queue_position_seq;
"""


async def init_pool() -> asyncpg.Pool:
    """Create the connection pool and ensure schema exists. Call once at startup."""
    global _pool
    _pool = await asyncpg.create_pool(dsn=config.DATABASE_URL)
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA)
    return _pool


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_pool() first.")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
