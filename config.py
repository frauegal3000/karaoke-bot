"""Environment variable loading and validation."""

import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it in your environment or in a .env file."
        )
    return value


TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
DATABASE_URL = _require("DATABASE_URL")
ADMIN_PASSWORD = _require("ADMIN_PASSWORD")

# Queue submission limits (see spec: "Songs per user")
MAX_SONGS_WHEN_QUEUE_SMALL = 2
MAX_SONGS_WHEN_QUEUE_LARGE = 1
QUEUE_SIZE_THRESHOLD = 10
